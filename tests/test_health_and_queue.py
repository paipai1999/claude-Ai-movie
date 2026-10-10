import os
import sys
import unittest
import tempfile
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from web_ui import (
    system_health_check,
    get_job_queue,
    delete_from_queue,
    job_queue,
    queue_lock,
    jobs,
    jobs_lock,
)
from agents.voice_agent import VoiceAgent
from agents.qa_agent import QAAgent


class TestHealthAndQueue(unittest.TestCase):

    def setUp(self):
        with queue_lock:
            job_queue.clear()

    def tearDown(self):
        with queue_lock:
            job_queue.clear()
        import shutil
        for t_dir in ["test_movie_1", "test_movie_2"]:
            p = os.path.join("outputs", t_dir)
            if os.path.exists(p):
                shutil.rmtree(p, ignore_errors=True)

    def test_system_health_check_structure(self):
        with patch('brain.config.load_config', return_value={'gemini': {'api_keys': ['dummy_key_123']}}):
            with patch('urllib.request.urlopen') as mock_url:
                mock_resp = MagicMock()
                mock_resp.getcode.return_value = 200
                mock_url.return_value = mock_resp

                import asyncio
                res = asyncio.run(system_health_check())
                self.assertIn('status', res)
                self.assertIn('checks', res)
                checks = res['checks']
                self.assertIn('ffmpeg', checks)
                self.assertIn('gemini', checks)
                self.assertIn('edge_tts', checks)
                self.assertIn('disk', checks)
                self.assertIn('installed', checks['ffmpeg'])
                self.assertIn('healthy', checks['gemini'])
                self.assertIn('healthy', checks['disk'])

    def test_queue_list_and_remove(self):
        with queue_lock:
            job_queue.append({
                'job_id': 'test_job_1',
                'source': 'https://example.com/video1.mp4',
                'language': 'burmese',
                'tts_engine': 'edge_tts',
                'status': 'queued',
                'target': None,
                'args': (),
                'kwargs': {}
            })
            job_queue.append({
                'job_id': 'test_job_2',
                'source': 'https://example.com/video2.mp4',
                'language': 'english',
                'tts_engine': 'f5_tts',
                'status': 'queued',
                'target': None,
                'args': (),
                'kwargs': {}
            })

        q_info = get_job_queue()
        self.assertEqual(q_info['queue_length'], 2)
        self.assertEqual(len(q_info['queue']), 2)
        self.assertEqual(q_info['queue'][0]['job_id'], 'test_job_1')

        del_res = delete_from_queue('test_job_1')
        self.assertTrue(del_res['success'])

        q_info2 = get_job_queue()
        self.assertEqual(q_info2['queue_length'], 1)
        self.assertEqual(q_info2['queue'][0]['job_id'], 'test_job_2')

    def test_f5_tts_burmese_allow_config(self):
        from brain.memory import MovieState
        burmese_text = chr(0x1000) + chr(0x103a) + chr(0x101b) + ' ' + chr(0x1019) + chr(0x103c) + chr(0x1014) + chr(0x103a) + chr(0x1019) + chr(0x102c)

        # 1. allow_burmese=False (default): should fallback to _generate_edge_voiceover
        agent = VoiceAgent(engine="f5_tts")
        agent.f5_cfg = {"allow_burmese": False, "force_engine": False}
        state = MovieState(movie_name="test_movie_1", video_path="dummy.mp4", project_dir="test_proj_1", language="burmese")
        state.generated_script = [{"scene_id": 1, "narration": burmese_text}]

        with patch.object(agent, '_generate_edge_voiceover', return_value=state) as mock_edge:
            with patch.object(agent, '_generate_f5_voiceover', return_value=state) as mock_f5:
                agent.generate_voiceover(state)
                self.assertTrue(mock_edge.called)
                self.assertFalse(mock_f5.called)

        # 2. allow_burmese=True: should route to _generate_f5_voiceover
        agent2 = VoiceAgent(engine="f5_tts")
        agent2.engine = "f5_tts"
        agent2.f5_cfg = {"allow_burmese": True, "force_engine": False}
        agent2.f5_engine = MagicMock()
        agent2.f5_engine.is_available.return_value = True
        state2 = MovieState(movie_name="test_movie_2", video_path="dummy.mp4", project_dir="test_proj_2", language="burmese")
        state2.generated_script = [{"scene_id": 1, "narration": burmese_text}]

        with patch.object(agent2, '_generate_edge_voiceover', return_value=state2) as mock_edge2:
            with patch.object(agent2, '_generate_f5_voiceover', return_value=state2) as mock_f5_2:
                agent2.generate_voiceover(state2)
                self.assertTrue(mock_f5_2.called)
                self.assertFalse(mock_edge2.called)

    def test_qa_agent_preview_compression(self):
        qa = QAAgent()
        temp_dir = tempfile.gettempdir()

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tf:
            tf.write(b'0' * 1024)
            dummy_path = tf.name

        try:
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                expected_preview = os.path.join(temp_dir, 'temp_qa_preview_360p.mp4')
                with patch('os.path.exists', return_value=True):
                    with patch('os.path.getsize', return_value=5 * 1024 * 1024):
                        comp_path = qa._create_qa_preview(dummy_path, temp_dir)
                        self.assertTrue(mock_run.called)
                        self.assertEqual(comp_path, expected_preview)

            with patch('subprocess.run', side_effect=Exception('ffmpeg failed')):
                fallback = qa._create_qa_preview(dummy_path, temp_dir)
                self.assertIsNone(fallback)
        finally:
            if os.path.exists(dummy_path):
                os.remove(dummy_path)

    def test_active_job_metadata_and_stop_purge(self):
        import asyncio
        from web_ui import get_active_job, stop_pipeline

        with jobs_lock:
            jobs['job_active_meta'] = {
                'status': 'running',
                'phase': 'Processing Phase 2',
                'name': 'super_movie.mp4',
                'source': 'super_movie.mp4',
                'language': 'burmese',
                'tts_engine': 'edge_tts',
                'created_at': 1234567.0
            }
        with queue_lock:
            job_queue.append({
                'job_id': 'job_queued_meta',
                'name': 'queued_video.mp4',
                'source': 'queued_video.mp4',
                'language': 'english',
                'tts_engine': 'f5_tts',
                'status': 'queued',
                'target': MagicMock(),
                'args': ()
            })

        # 1. Verify get_active_job returns full metadata
        active = get_active_job()
        self.assertEqual(active['job_id'], 'job_active_meta')
        self.assertEqual(active['language'], 'burmese')
        self.assertEqual(active['source'], 'super_movie.mp4')
        self.assertEqual(active['tts_engine'], 'edge_tts')

        # 2. Verify stop_pipeline purges queue
        res = asyncio.run(stop_pipeline())
        self.assertTrue(res['success'])
        with queue_lock:
            self.assertEqual(len(job_queue), 0)
        with jobs_lock:
            self.assertEqual(jobs['job_active_meta']['status'], 'cancelled')
            jobs.pop('job_active_meta', None)


if __name__ == '__main__':
    unittest.main()
