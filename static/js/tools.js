/* ==========================================================================
   Pai AI Movie Studio - tools
   ==========================================================================
   The tool hub (home), the two tool panels (Long to Short, Splitter) and the result viewer.

     - the hub: one tile per way of working; a tile opens the production wizard (with the delivery mode
       already chosen) or a tool panel;
     - a tool panel is GENERATED from the option model (GET /api/options): every control is one option of
       the tool's engine, so the panel can never drift from the API or the command line;
     - a run is started with POST /api/start and followed with GET /api/status/{id};
     - the result viewer shows the Shorts / parts a run made (GET /api/projects/{name}/tool-result), from
       a finished run or from the Projects view.

   Helpers (el, api, toast, ...) come from studio.js (PaiStudio.ui). All text goes through textContent /
   DOM nodes - nothing from the server is ever injected as HTML.
   ========================================================================== */
(function () {
    'use strict';

    const studio = window.PaiStudio;
    if (!studio || !studio.ui) { console.warn('tools.js needs studio.js'); return; }
    const { el, api, toast, clock, lang } = studio.ui;
    const $ = id => document.getElementById(id);

    // ── texts ─────────────────────────────────────────────────────────────────
    const TEXT = {
        en: {
            hubTitle: 'Choose how you want to work',
            tileRecap: 'Recap Studio', tileRecapDesc: 'Any film or drama -> a Burmese narrated recap, rendered start to end in one click.',
            tileCapcut: 'Recap -> CapCut', tileCapcutDesc: 'The same recap, but the slow render is skipped: you get a CapCut package (narration track + captions) to finish the edit.',
            tileSubtitle: 'Subtitle Studio', tileSubtitleDesc: 'A video -> Burmese subtitles and transcripts (.srt / .ass / .txt).',
            tileHardsub: 'Hardsub Studio', tileHardsubDesc: 'Keeps the original audio and burns Burmese subtitles into the picture.',
            tileShorts: 'Long to Short', tileShortsDesc: 'A long video -> the best moments as vertical Shorts / Reels with subtitles, titles and captions.',
            tileSplitter: 'Splitter', tileSplitterDesc: 'Cut a long video into parts by minutes, or into equal parts, at a pause in the speech.',
            badgeNew: 'New',
            backToTools: '← Tools', title_shorts: 'Long to Short', title_splitter: 'Splitter', title_result: 'Results',
            desc_shorts: 'The AI reads the whole video, picks the best moments and makes each one a finished vertical clip - or a clean clip to finish in CapCut.',
            desc_splitter: 'Cuts a long video into parts. Nothing about the picture or sound changes.',
            source: 'Video link or file', sourcePh: 'https://www.youtube.com/watch?v=...  or  movies/film.mp4', paste: 'Paste', upload: 'Upload',
            advanced: 'More options', start: 'Start', starting: 'Starting...', stop: 'Stop', again: 'Run again', newRun: 'New run',
            needInput: 'Enter a video link or choose a file first.', needRange: 'The shortest clip cannot be longer than the longest.',
            uploading: 'Uploading...', uploaded: 'Uploaded: {name}', uploadFailed: 'Upload failed', pasteFailed: 'Clipboard paste is unavailable. Paste manually.',
            queued: 'Waiting in the queue (position {n})...', running: 'Working...', elapsed: 'Elapsed {time}', done: 'Done', failed: 'Failed',
            cancelled: 'Stopped', startFailed: 'Could not start',
            opt_project_name: 'Project name (optional)', opt_resolution: 'Quality', opt_source_language: 'Spoken language (auto = detect)',
            opt_context_hint: 'What makes a good clip for you? (optional)', hint_context_hint: 'e.g. "I make cooking videos; I like funny moments and quick tips".',
            opt_subtitle_style: 'Subtitle style', opt_video_format: 'Format',
            opt_split_mode: 'Split by', opt_split_minutes: 'Minutes per part', opt_split_parts: 'Number of parts',
            opt_split_snap: 'Cut at a pause, not mid-sentence', hint_split_snap: 'Moves every cut to the nearest quiet moment in the audio.',
            opt_split_reencode: 'Frame-accurate cuts (slower)', hint_split_reencode: 'Off: instant, lossless cuts on keyframes. On: every part is re-encoded.',
            opt_clip_count: 'How many Shorts', opt_clip_min_sec: 'Shortest clip (seconds)', opt_clip_max_sec: 'Longest clip (seconds)',
            opt_clip_focus: 'Look for', opt_short_subtitles: 'Subtitles', opt_short_hook_title: 'Show the title over the first seconds',
            hint_short_hook_title: 'A short, bold hook line at the top: it is what makes people keep watching.',
            opt_short_framing: 'Vertical framing',
            ch_split_mode_duration: 'Every N minutes', ch_split_mode_parts: 'N equal parts',
            ch_resolution_1080p: '1080p (Full HD)', ch_resolution_720p: '720p',
            ch_clip_focus_balanced: 'The strongest moments', ch_clip_focus_funny: 'Funny moments', ch_clip_focus_emotional: 'Emotional moments',
            ch_clip_focus_informative: 'Useful tips and facts', ch_clip_focus_action: 'Action and big reveals',
            ch_short_subtitles_burmese: 'Burmese (translated)', ch_short_subtitles_original: 'Original language', ch_short_subtitles_none: 'No subtitles',
            ch_short_framing_fit: 'Whole picture on a blurred background', ch_short_framing_fill: 'Fill the screen (centre crop)',
            ch_video_format_916: '9:16 vertical (TikTok / Reels / Shorts)', ch_video_format_169: '16:9 landscape', ch_video_format_both: 'Both',
            ch_subtitle_style_box_black: 'Black box', ch_subtitle_style_yellow_pop: 'Yellow pop', ch_subtitle_style_white_stroke: 'White outline',
            ch_subtitle_style_cyan_cyber: 'Cyan', ch_subtitle_style_crimson_box: 'Crimson box',
            shortsCapcutDesc: 'No final render: clean clips (nothing burnt in) with captions, in a package to finish in CapCut.',
            shortsBothDesc: 'Finished Shorts, and the clean clips + captions package.',
            resultShorts: '{n} Short(s) ready', resultParts: '{n} part(s) ready', resultEmpty: 'Nothing to show yet.', resultFailed: 'Could not load the result',
            play: 'Play', download: 'Download', cleanClip: 'Clean clip (CapCut)', subtitlesSrt: 'Subtitles (.srt)', cover: 'Cover',
            copyCaption: 'Copy caption', copyTags: 'Copy hashtags', copied: 'Copied', score: 'Score {n}', why: 'Why this moment', noCaption: 'No caption',
            zipAll: 'Download everything (ZIP)', capcutPackage: 'CapCut package', captionsList: 'Captions list', part: 'Part {n}', size: '{mb} MB',
            kindShort: 'Finished', projectsResults: 'Results', source_label: 'Source'
        },
        my: {
            hubTitle: 'ဘယ်လို အလုပ်လုပ်ချင်လဲ ရွေးပါ',
            tileRecap: 'Recap Studio', tileRecapDesc: 'ဘယ်ဇာတ်ကား/ဒရာမာမဆို -> မြန်မာ narration recap ကို one-click နဲ့ အစအဆုံး ပြီးအောင် ထုတ်ပေးမယ်။',
            tileCapcut: 'Recap -> CapCut', tileCapcutDesc: 'Recap အတူတူပဲ၊ ဒါပေမဲ့ နှေးတဲ့ render ကို ကျော်ပြီး CapCut package (narration track + စာတန်း) ရမယ်။ ကျန်တာ CapCut မှာ အပြီးသတ်ပါ။',
            tileSubtitle: 'Subtitle Studio', tileSubtitleDesc: 'ဗီဒီယို -> မြန်မာစာတန်းနဲ့ transcript (.srt / .ass / .txt)။',
            tileHardsub: 'Hardsub Studio', tileHardsubDesc: 'မူရင်းအသံကို ထိန်းပြီး မြန်မာစာတန်းကို ပုံထဲ ထည့်ပေးမယ်။',
            tileShorts: 'Long to Short', tileShortsDesc: 'ဗီဒီယိုရှည် -> အကောင်းဆုံး အပိုင်းတွေကို စာတန်း၊ ခေါင်းစဉ်၊ caption နဲ့ ဒေါင်လိုက် Shorts / Reels လုပ်ပေးမယ်။',
            tileSplitter: 'Splitter', tileSplitterDesc: 'ဗီဒီယိုရှည်ကို မိနစ်အလိုက် ဒါမှမဟုတ် ညီတူအပိုင်းတွေအဖြစ် စကားရပ်တဲ့နေရာမှာ ဖြတ်ပေးမယ်။',
            badgeNew: 'အသစ်',
            backToTools: '← Tools', title_shorts: 'Long to Short', title_splitter: 'Splitter', title_result: 'ရလဒ်များ',
            desc_shorts: 'AI က ဗီဒီယိုတစ်ခုလုံးကို ဖတ်ပြီး အကောင်းဆုံး အပိုင်းတွေကို ရွေးကာ တစ်ခုချင်းကို ဒေါင်လိုက် clip အပြီးသတ် (ဒါမှမဟုတ် CapCut မှာ ဆက်လုပ်ဖို့ clean clip) လုပ်ပေးမယ်။',
            desc_splitter: 'ဗီဒီယိုရှည်ကို အပိုင်းတွေ ဖြတ်ပေးမယ်။ ပုံနဲ့ အသံ ဘာမှ မပြောင်းပါဘူး။',
            source: 'ဗီဒီယို link သို့မဟုတ် ဖိုင်', sourcePh: 'https://www.youtube.com/watch?v=...  သို့မဟုတ်  movies/film.mp4', paste: 'Paste', upload: 'Upload',
            advanced: 'ထပ်ဆောင်း ရွေးချယ်မှုများ', start: 'စတင်မယ်', starting: 'စတင်နေသည်...', stop: 'ရပ်မယ်', again: 'ပြန် run မယ်', newRun: 'အသစ် run မယ်',
            needInput: 'ဗီဒီယို link ထည့်ပါ သို့မဟုတ် ဖိုင်ရွေးပါ။', needRange: 'အတိုဆုံး clip က အရှည်ဆုံးထက် မရှည်ရပါ။',
            uploading: 'Upload လုပ်နေသည်...', uploaded: 'Upload ပြီး: {name}', uploadFailed: 'Upload မအောင်မြင်ပါ', pasteFailed: 'Clipboard paste မရပါ၊ ကိုယ်တိုင် paste လုပ်ပါ။',
            queued: 'တန်းစီစောင့်နေသည် (နေရာ {n})...', running: 'လုပ်ဆောင်နေသည်...', elapsed: 'ကြာချိန် {time}', done: 'ပြီးပါပြီ', failed: 'မအောင်မြင်ပါ',
            cancelled: 'ရပ်လိုက်သည်', startFailed: 'စတင်၍ မရပါ',
            opt_project_name: 'Project အမည် (မဖြစ်မနေမလို)', opt_resolution: 'အရည်အသွေး', opt_source_language: 'ပြောတဲ့ ဘာသာစကား (auto = အလိုအလျောက်)',
            opt_context_hint: 'သင့်အတွက် ကောင်းတဲ့ clip ဆိုတာ ဘာလဲ? (မဖြစ်မနေမလို)', hint_context_hint: 'ဥပမာ - "ဟင်းချက်ဗီဒီယို လုပ်တယ်၊ ရယ်စရာအပိုင်းနဲ့ အကြံပြုချက်တိုတွေ ကြိုက်တယ်"။',
            opt_subtitle_style: 'စာတန်း ပုံစံ', opt_video_format: 'Format',
            opt_split_mode: 'ဖြတ်ပုံ', opt_split_minutes: 'အပိုင်းတစ်ခုအတွက် မိနစ်', opt_split_parts: 'အပိုင်း အရေအတွက်',
            opt_split_snap: 'စကားရပ်တဲ့နေရာမှာ ဖြတ်မယ် (စာကြောင်းလယ် မဖြတ်)', hint_split_snap: 'ဖြတ်ချက်တိုင်းကို အသံထဲက အနီးဆုံး တိတ်ဆိတ်တဲ့ နေရာကို ရွှေ့ပေးမယ်။',
            opt_split_reencode: 'Frame တိကျတဲ့ ဖြတ်ချက် (နှေးမယ်)', hint_split_reencode: 'ပိတ်: ချက်ချင်း၊ အရည်အသွေးမကျ၊ keyframe မှာ ဖြတ်။ ဖွင့်: အပိုင်းတိုင်းကို ပြန် encode လုပ်မယ်။',
            opt_clip_count: 'Shorts ဘယ်နှစ်ခု', opt_clip_min_sec: 'အတိုဆုံး clip (စက္ကန့်)', opt_clip_max_sec: 'အရှည်ဆုံး clip (စက္ကန့်)',
            opt_clip_focus: 'ရှာမယ့် အမျိုးအစား', opt_short_subtitles: 'စာတန်း', opt_short_hook_title: 'ပထမစက္ကန့်တွေမှာ ခေါင်းစဉ် ပြမယ်',
            hint_short_hook_title: 'အပေါ်ဆုံးမှာ စာတိုပြချက် - လူတွေ ဆက်ကြည့်စေတဲ့ အရာပါ။',
            opt_short_framing: 'ဒေါင်လိုက် framing',
            ch_split_mode_duration: 'N မိနစ်တိုင်း', ch_split_mode_parts: 'ညီတူ N ပိုင်း',
            ch_resolution_1080p: '1080p (Full HD)', ch_resolution_720p: '720p',
            ch_clip_focus_balanced: 'အကောင်းဆုံး အပိုင်းတွေ', ch_clip_focus_funny: 'ရယ်စရာ အပိုင်း', ch_clip_focus_emotional: 'ခံစားချက်ပြင်းတဲ့ အပိုင်း',
            ch_clip_focus_informative: 'အသုံးဝင်တဲ့ အကြံ၊ အချက်အလက်', ch_clip_focus_action: 'အက်ရှင်နဲ့ အံ့အားသင့်စရာ',
            ch_short_subtitles_burmese: 'မြန်မာ (ဘာသာပြန်)', ch_short_subtitles_original: 'မူရင်းဘာသာ', ch_short_subtitles_none: 'စာတန်း မထည့်',
            ch_short_framing_fit: 'ပုံအပြည့်ကို ဝါးထားတဲ့ နောက်ခံပေါ်', ch_short_framing_fill: 'မျက်နှာပြင်အပြည့် (အလယ်ကို ဖြတ်)',
            ch_video_format_916: '9:16 ဒေါင်လိုက် (TikTok / Reels / Shorts)', ch_video_format_169: '16:9 အလျားလိုက်', ch_video_format_both: 'နှစ်မျိုးလုံး',
            ch_subtitle_style_box_black: 'အနက်ရောင် box', ch_subtitle_style_yellow_pop: 'အဝါရောင်', ch_subtitle_style_white_stroke: 'အဖြူ အနားသတ်',
            ch_subtitle_style_cyan_cyber: 'Cyan', ch_subtitle_style_crimson_box: 'အနီရင့် box',
            shortsCapcutDesc: 'နောက်ဆုံး render မပါ - စာတန်းမထည့်ရသေးတဲ့ clean clip တွေ + စာတန်း package၊ CapCut မှာ အပြီးသတ်ပါ။',
            shortsBothDesc: 'ပြီးသား Shorts ရော၊ clean clip + စာတန်း package ပါ ရမယ်။',
            resultShorts: 'Shorts {n} ခု အဆင်သင့်', resultParts: 'အပိုင်း {n} ခု အဆင်သင့်', resultEmpty: 'ပြစရာ မရှိသေးပါ။', resultFailed: 'ရလဒ်ကို မဖတ်နိုင်ပါ',
            play: 'ကြည့်မယ်', download: 'ဒေါင်းလုဒ်', cleanClip: 'Clean clip (CapCut)', subtitlesSrt: 'စာတန်း (.srt)', cover: 'Cover',
            copyCaption: 'Caption ကူးမယ်', copyTags: 'Hashtag ကူးမယ်', copied: 'ကူးပြီး', score: 'ရမှတ် {n}', why: 'ဒီအပိုင်းကို ဘာကြောင့် ရွေးလဲ', noCaption: 'Caption မရှိ',
            zipAll: 'အားလုံး ဒေါင်းလုဒ် (ZIP)', capcutPackage: 'CapCut package', captionsList: 'Caption စာရင်း', part: 'အပိုင်း {n}', size: '{mb} MB',
            kindShort: 'ပြီးသား', projectsResults: 'ရလဒ်များ', source_label: 'မူရင်း'
        },
        th: {
            hubTitle: 'เลือกวิธีทำงาน',
            tileRecap: 'Recap Studio', tileRecapDesc: 'หนังหรือซีรีส์ใดก็ได้ -> สรุปเรื่องพร้อมเสียงบรรยายพม่า เรนเดอร์ครบในคลิกเดียว',
            tileCapcut: 'Recap -> CapCut', tileCapcutDesc: 'สรุปเรื่องแบบเดิม แต่ข้ามการเรนเดอร์ที่ช้า ได้แพ็กเกจ CapCut (แทร็กเสียง + คำบรรยาย) ไปตัดต่อต่อ',
            tileSubtitle: 'Subtitle Studio', tileSubtitleDesc: 'วิดีโอ -> คำบรรยายและสคริปต์ภาษาพม่า (.srt / .ass / .txt)',
            tileHardsub: 'Hardsub Studio', tileHardsubDesc: 'คงเสียงต้นฉบับและฝังคำบรรยายพม่าลงในภาพ',
            tileShorts: 'Long to Short', tileShortsDesc: 'วิดีโอยาว -> ช่วงที่ดีที่สุดเป็น Shorts / Reels แนวตั้ง พร้อมคำบรรยาย ชื่อ และแคปชัน',
            tileSplitter: 'Splitter', tileSplitterDesc: 'ตัดวิดีโอยาวเป็นส่วนตามนาที หรือแบ่งเท่า ๆ กัน ตรงช่วงที่เงียบ',
            badgeNew: 'ใหม่',
            backToTools: '← Tools', title_shorts: 'Long to Short', title_splitter: 'Splitter', title_result: 'ผลลัพธ์',
            desc_shorts: 'AI อ่านทั้งวิดีโอ เลือกช่วงที่ดีที่สุด แล้วทำเป็นคลิปแนวตั้งสำเร็จรูป หรือคลิปสะอาดไปตัดต่อใน CapCut',
            desc_splitter: 'ตัดวิดีโอยาวเป็นส่วน ๆ ภาพและเสียงไม่เปลี่ยน',
            source: 'ลิงก์วิดีโอหรือไฟล์', sourcePh: 'https://www.youtube.com/watch?v=...  หรือ  movies/film.mp4', paste: 'วาง', upload: 'อัปโหลด',
            advanced: 'ตัวเลือกเพิ่มเติม', start: 'เริ่ม', starting: 'กำลังเริ่ม...', stop: 'หยุด', again: 'รันอีกครั้ง', newRun: 'รันใหม่',
            needInput: 'ใส่ลิงก์วิดีโอหรือเลือกไฟล์ก่อน', needRange: 'คลิปที่สั้นที่สุดต้องไม่ยาวกว่าคลิปที่ยาวที่สุด',
            uploading: 'กำลังอัปโหลด...', uploaded: 'อัปโหลดแล้ว: {name}', uploadFailed: 'อัปโหลดไม่สำเร็จ', pasteFailed: 'วางจากคลิปบอร์ดไม่ได้ โปรดวางเอง',
            queued: 'รอคิว (ลำดับ {n})...', running: 'กำลังทำงาน...', elapsed: 'ใช้เวลา {time}', done: 'เสร็จแล้ว', failed: 'ไม่สำเร็จ',
            cancelled: 'หยุดแล้ว', startFailed: 'เริ่มไม่ได้',
            opt_project_name: 'ชื่อโปรเจกต์ (ไม่บังคับ)', opt_resolution: 'คุณภาพ', opt_source_language: 'ภาษาที่พูด (auto = ตรวจจับเอง)',
            opt_context_hint: 'คลิปที่ดีสำหรับคุณคืออะไร (ไม่บังคับ)', hint_context_hint: 'เช่น "ทำคลิปทำอาหาร ชอบช่วงตลกและเคล็ดลับสั้น ๆ"',
            opt_subtitle_style: 'รูปแบบคำบรรยาย', opt_video_format: 'รูปแบบ',
            opt_split_mode: 'แบ่งตาม', opt_split_minutes: 'นาทีต่อส่วน', opt_split_parts: 'จำนวนส่วน',
            opt_split_snap: 'ตัดตรงช่วงเงียบ ไม่ตัดกลางประโยค', hint_split_snap: 'ย้ายทุกจุดตัดไปยังช่วงเงียบที่ใกล้ที่สุดในเสียง',
            opt_split_reencode: 'ตัดแม่นระดับเฟรม (ช้ากว่า)', hint_split_reencode: 'ปิด: ทันที ไม่เสียคุณภาพ ตัดที่คีย์เฟรม เปิด: เข้ารหัสใหม่ทุกส่วน',
            opt_clip_count: 'จำนวน Shorts', opt_clip_min_sec: 'คลิปสั้นที่สุด (วินาที)', opt_clip_max_sec: 'คลิปยาวที่สุด (วินาที)',
            opt_clip_focus: 'มองหา', opt_short_subtitles: 'คำบรรยาย', opt_short_hook_title: 'แสดงชื่อในไม่กี่วินาทีแรก',
            hint_short_hook_title: 'ข้อความสั้นเด่นที่ด้านบน ช่วยให้คนดูต่อ',
            opt_short_framing: 'การจัดเฟรมแนวตั้ง',
            ch_split_mode_duration: 'ทุก N นาที', ch_split_mode_parts: 'N ส่วนเท่า ๆ กัน',
            ch_resolution_1080p: '1080p (Full HD)', ch_resolution_720p: '720p',
            ch_clip_focus_balanced: 'ช่วงที่ดีที่สุด', ch_clip_focus_funny: 'ช่วงตลก', ch_clip_focus_emotional: 'ช่วงอารมณ์แรง',
            ch_clip_focus_informative: 'เคล็ดลับและข้อเท็จจริงที่มีประโยชน์', ch_clip_focus_action: 'แอ็กชันและการเฉลย',
            ch_short_subtitles_burmese: 'พม่า (แปลแล้ว)', ch_short_subtitles_original: 'ภาษาต้นฉบับ', ch_short_subtitles_none: 'ไม่มีคำบรรยาย',
            ch_short_framing_fit: 'ภาพเต็มบนพื้นหลังเบลอ', ch_short_framing_fill: 'เต็มหน้าจอ (ครอปกลาง)',
            ch_video_format_916: '9:16 แนวตั้ง (TikTok / Reels / Shorts)', ch_video_format_169: '16:9 แนวนอน', ch_video_format_both: 'ทั้งสองแบบ',
            ch_subtitle_style_box_black: 'กล่องดำ', ch_subtitle_style_yellow_pop: 'เหลืองเด่น', ch_subtitle_style_white_stroke: 'ขอบขาว',
            ch_subtitle_style_cyan_cyber: 'ฟ้าไซเบอร์', ch_subtitle_style_crimson_box: 'กล่องแดงเข้ม',
            shortsCapcutDesc: 'ไม่เรนเดอร์ขั้นสุดท้าย: คลิปสะอาด (ไม่ฝังข้อความ) พร้อมคำบรรยายในแพ็กเกจไปตัดต่อใน CapCut',
            shortsBothDesc: 'ได้ Shorts สำเร็จรูป และแพ็กเกจคลิปสะอาด + คำบรรยาย',
            resultShorts: 'Shorts พร้อม {n} คลิป', resultParts: 'พร้อม {n} ส่วน', resultEmpty: 'ยังไม่มีอะไรให้แสดง', resultFailed: 'โหลดผลลัพธ์ไม่ได้',
            play: 'เล่น', download: 'ดาวน์โหลด', cleanClip: 'คลิปสะอาด (CapCut)', subtitlesSrt: 'คำบรรยาย (.srt)', cover: 'ภาพปก',
            copyCaption: 'คัดลอกแคปชัน', copyTags: 'คัดลอกแฮชแท็ก', copied: 'คัดลอกแล้ว', score: 'คะแนน {n}', why: 'ทำไมถึงเลือกช่วงนี้', noCaption: 'ไม่มีแคปชัน',
            zipAll: 'ดาวน์โหลดทั้งหมด (ZIP)', capcutPackage: 'แพ็กเกจ CapCut', captionsList: 'รายการแคปชัน', part: 'ส่วนที่ {n}', size: '{mb} MB',
            kindShort: 'สำเร็จรูป', projectsResults: 'ผลลัพธ์', source_label: 'ต้นฉบับ'
        }
    };

    function tt(key, vars) {
        const dict = TEXT[lang()] || TEXT.en;
        let text = dict[key] !== undefined ? dict[key] : (TEXT.en[key] !== undefined ? TEXT.en[key] : key);
        for (const [name, value] of Object.entries(vars || {})) text = text.split('{' + name + '}').join(String(value));
        return text;
    }

    // ── the tools: which options each panel shows, and in which order ─────────────
    // Every name is an option of the engine in core/options.py; the panel reads type, default and choices from the model.
    const TOOLS = {
        shorts: {
            engine: 'shorts',
            icon: 'M8 5v14l11-7z',
            basic: ['clip_count', 'clip_focus', 'short_subtitles', 'delivery_mode', 'video_format', 'short_hook_title'],
            advanced: ['clip_min_sec', 'clip_max_sec', 'short_framing', 'subtitle_style', 'source_language', 'context_hint', 'resolution', 'project_name']
        },
        splitter: {
            engine: 'splitter',
            icon: 'M6 3v18M18 3v18M6 12h12',
            basic: ['split_mode', 'split_minutes', 'split_parts', 'split_snap'],
            advanced: ['split_reencode', 'resolution', 'project_name']
        }
    };

    // [min, max, step] for the number controls
    const RANGES = { clip_count: [1, 20, 1], clip_min_sec: [8, 170, 1], clip_max_sec: [13, 180, 1], split_minutes: [0.5, 600, 0.5], split_parts: [2, 99, 1] };
    const CHOICE_KEY = { '9:16': '916', '16:9': '169' };      // option values that are not safe in a text key

    const state = { tool: null, model: null, controls: {}, jobId: null, timer: null, view: 'form' };

    async function loadModel() {
        if (state.model) return state.model;
        try { state.model = await api('/api/options'); }
        catch (e) { console.warn('Option model unavailable', e); state.model = { options: {} }; }
        return state.model;
    }

    const spec = name => (state.model && state.model.options[name]) || null;
    const defaultOf = (name, engine) => {
        const s = spec(name);
        if (!s) return null;
        const per = s.engine_defaults || {};
        return engine in per ? per[engine] : s.default;
    };

    // ── hub ───────────────────────────────────────────────────────────────────
    const TILES = [
        { id: 'recap', tone: 'gold', icon: 'M5 3l14 9-14 9z', title: 'tileRecap', desc: 'tileRecapDesc', open: () => startWizard('recap', 'oneclick'), ids: ['card-engine1-title', 'card-engine1-desc'] },
        { id: 'capcut', tone: 'blue', icon: 'M6 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM6 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM8.1 7.9L20 20M8.1 16.1L20 4', title: 'tileCapcut', desc: 'tileCapcutDesc', open: () => startWizard('recap', 'capcut') },
        { id: 'subtitle', tone: 'green', icon: 'M3 6h18v12H3zM7 11h6M7 14h10', title: 'tileSubtitle', desc: 'tileSubtitleDesc', open: () => startWizard('subtitle', 'oneclick'), ids: ['card-engine2-title', 'card-engine2-desc'] },
        { id: 'hardsub', tone: 'violet', icon: 'M4 5h16v11H4zM8 20h8M12 16v4', title: 'tileHardsub', desc: 'tileHardsubDesc', open: () => startWizard('hardsub', 'oneclick'), ids: ['card-engine3-title', 'card-engine3-desc'] },
        { id: 'shorts', tone: 'rose', icon: TOOLS.shorts.icon, title: 'tileShorts', desc: 'tileShortsDesc', open: () => openTool('shorts'), fresh: true },
        { id: 'splitter', tone: 'amber', icon: TOOLS.splitter.icon, title: 'tileSplitter', desc: 'tileSplitterDesc', open: () => openTool('splitter'), fresh: true }
    ];

    function svg(path) {
        const ns = 'http://www.w3.org/2000/svg';
        const node = document.createElementNS(ns, 'svg');
        node.setAttribute('viewBox', '0 0 24 24');
        node.setAttribute('aria-hidden', 'true');
        const p = document.createElementNS(ns, 'path');
        p.setAttribute('d', path);
        node.append(p);
        return node;
    }

    function renderHub() {
        const grid = $('tool-grid');
        if (!grid) return;
        const title = $('tool-hub-title');
        if (title) title.textContent = tt('hubTitle');
        grid.replaceChildren(...TILES.map(tile => {
            const heading = el('div', { class: 'tool-tile-title', text: tt(tile.title) });
            const description = el('div', { class: 'tool-tile-desc', text: tt(tile.desc) });
            if (tile.ids) { heading.id = tile.ids[0]; description.id = tile.ids[1]; }
            return el('button', { type: 'button', class: 'tool-tile tone-' + tile.tone, 'data-tool': tile.id, onclick: tile.open },
                el('span', { class: 'tool-tile-icon' }, svg(tile.icon)),
                tile.fresh ? el('span', { class: 'chip chip-gold tool-tile-badge', text: tt('badgeNew') }) : null,
                heading, description);
        }));
    }

    /** Opens the production wizard on step 1 with the engine and the delivery mode already chosen. */
    function startWizard(engine, delivery) {
        window.switchView('studio');
        const select = $('select-engine-mode');
        if (select) { select.value = engine; if (typeof window.handleEngineChange === 'function') window.handleEngineChange(engine); }
        const radio = document.querySelector('input[name="delivery_mode"][value="' + delivery + '"]');
        if (radio) { radio.checked = true; if (typeof window.handleDeliveryChange === 'function') window.handleDeliveryChange(); }
        window.goToStep(1);
    }

    // ── tool panel ──────────────────────────────────────────────────────────────
    function choiceLabel(name, value) {
        const key = 'ch_' + name + '_' + (CHOICE_KEY[value] || value);
        const dict = TEXT[lang()] || TEXT.en;
        return dict[key] !== undefined || TEXT.en[key] !== undefined ? tt(key) : String(value);
    }

    function field(label, control, hint) {
        return el('div', { class: 'tool-field' }, el('label', { class: 'form-label', text: label }), control,
            hint ? el('div', { class: 'tool-hint', text: hint }) : null);
    }

    /** One control for one option. Returns { node, read, set, wrap }. */
    function buildControl(tool, name) {
        const s = spec(name);
        const engine = TOOLS[tool].engine;
        const initial = defaultOf(name, engine);
        const hint = TEXT.en['hint_' + name] ? tt('hint_' + name) : '';
        const label = tt('opt_' + name);

        if (name === 'delivery_mode') return buildDeliveryPicker(tool, initial);
        if (!s) return null;

        if (s.type === 'bool') {
            const input = el('input', { type: 'checkbox', checked: Boolean(initial) });
            const wrap = el('div', { class: 'tool-field' },
                el('label', { class: 'tool-switch' }, input, el('span', { class: 'tool-switch-track' }), el('span', { class: 'tool-switch-label', text: label })),
                hint ? el('div', { class: 'tool-hint', text: hint }) : null);
            return { wrap, read: () => input.checked, set: v => { input.checked = Boolean(v); }, input };
        }
        if (s.choices && s.choices.length) {
            const input = el('select', { class: 'select-control' }, s.choices.map(c => el('option', { value: c, text: choiceLabel(name, c), selected: c === initial })));
            input.value = initial;
            return { wrap: field(label, input, hint), read: () => input.value, set: v => { input.value = v; }, input };
        }
        if (s.type === 'int' || s.type === 'float') {
            const [min, max, step] = RANGES[name] || [0, 9999, 1];
            const input = el('input', { class: 'input-text', type: 'number', min, max, step, value: initial });
            const read = () => {
                const n = s.type === 'int' ? parseInt(input.value, 10) : parseFloat(input.value);
                return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : initial;
            };
            return { wrap: field(label, input, hint), read, set: v => { input.value = v; }, input };
        }
        const multiline = name === 'context_hint';
        const input = el(multiline ? 'textarea' : 'input', Object.assign({ class: multiline ? 'textarea-control' : 'input-text', placeholder: name === 'source_language' ? 'auto, en, zh, ko, ja, th' : '' },
            multiline ? { rows: 3 } : { type: 'text', value: initial || '' }));
        if (multiline) input.value = initial || '';
        return { wrap: field(label, input, hint), read: () => (input.value || '').trim() || null, set: v => { input.value = v || ''; }, input };
    }

    /** The three delivery cards (same look as the wizard; own radio group so the two never clash). */
    function buildDeliveryPicker(tool, initial) {
        const mode = ['oneclick', 'capcut', 'both'].includes(initial) ? initial : 'oneclick';
        const descKey = { oneclick: 'optDeliveryOneclickDesc', capcut: 'shortsCapcutDesc', both: 'shortsBothDesc' };
        const nameKey = { oneclick: 'optDeliveryOneclick', capcut: 'optDeliveryCapcut', both: 'optDeliveryBoth' };
        const icons = {
            oneclick: 'M5 3l14 9-14 9z',
            capcut: 'M6 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM6 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM8.1 7.9L20 20M8.1 16.1L20 4',
            both: 'M4 4h10v10H4zM10 10h10v10H10z'
        };
        const group = 'tool_delivery_' + tool;
        const cards = ['oneclick', 'capcut', 'both'].map(value => {
            const input = el('input', { type: 'radio', name: group, value, checked: value === mode });
            const card = el('label', { class: 'delivery-card' + (value === mode ? ' is-selected' : '') }, input,
                el('span', { class: 'delivery-icon' }, svg(icons[value])),
                el('span', { class: 'delivery-name', text: studio.t(nameKey[value]) }),
                el('span', { class: 'delivery-desc', text: value === 'oneclick' ? studio.t(descKey[value]) : tt(descKey[value]) }));
            input.addEventListener('change', () => cards.forEach(c => c.classList.toggle('is-selected', c.querySelector('input').checked)));
            return card;
        });
        const wrap = el('div', { class: 'delivery-picker tool-field' }, el('div', { class: 'delivery-title', text: studio.t('optDelivery') }), el('div', { class: 'delivery-cards' }, cards));
        const read = () => { const checked = wrap.querySelector('input:checked'); return checked ? checked.value : 'oneclick'; };
        const set = v => { const radio = wrap.querySelector('input[value="' + v + '"]'); if (radio) { radio.checked = true; cards.forEach(c => c.classList.toggle('is-selected', c.querySelector('input').checked)); } };
        return { wrap, read, set };
    }

    function stopPolling() {
        if (state.timer) { clearInterval(state.timer); state.timer = null; }
    }

    function root() { return $('view-tool'); }

    function header(titleKey, descKey) {
        return el('div', { class: 'tool-head' },
            el('button', { type: 'button', class: 'btn-nav btn-sm', text: tt('backToTools'), onclick: () => { stopPolling(); window.switchView('welcome'); } }),
            el('div', { style: 'min-width:0;flex:1;' }, el('div', { class: 'card-title', text: tt(titleKey) }), descKey ? el('div', { class: 'card-desc', text: tt(descKey) }) : null));
    }

    async function openTool(tool) {
        await loadModel();
        stopPolling();
        state.tool = tool;
        state.controls = {};
        state.jobId = null;
        state.view = 'form';
        window.switchView('tool');
        const titleEl = $('header-title-text');
        if (titleEl) titleEl.textContent = tt('title_' + tool);
        renderForm();
    }

    function renderForm() {
        const tool = state.tool;
        const plan = TOOLS[tool];
        const container = root();
        if (!container) return;
        const values = {};
        Object.keys(state.controls).forEach(k => { values[k] = state.controls[k].read(); });

        const input = el('input', { type: 'text', id: 'tool-input', class: 'input-text', placeholder: tt('sourcePh'), autocomplete: 'off' });
        const fileInput = el('input', { type: 'file', accept: 'video/*', style: 'display:none;', onchange: event => uploadFile(event.target, input) });
        const sourceRow = el('div', { class: 'tool-field' }, el('label', { class: 'form-label', text: tt('source') }),
            el('div', { class: 'row' }, input,
                el('button', { type: 'button', class: 'btn-secondary', id: 'tool-paste', text: tt('paste'), onclick: () => pasteInto(input) }),
                fileInput,
                el('button', { type: 'button', class: 'btn-secondary', id: 'tool-upload', text: tt('upload'), onclick: () => fileInput.click() })));

        state.controls = {};
        const make = names => names.map(name => {
            const control = buildControl(tool, name);
            if (!control) return null;
            state.controls[name] = control;
            if (name in values) control.set(values[name]);
            return control.wrap;
        });
        const basic = make(plan.basic);
        const advanced = make(plan.advanced);

        const start = el('button', { type: 'button', id: 'tool-start', class: 'btn-success tool-start', text: tt('start'), onclick: () => startRun(input.value.trim(), start) });
        const form = el('div', { class: 'card tool-panel', id: 'tool-form' },
            header('title_' + tool, 'desc_' + tool),
            el('div', { class: 'tool-body' },
                sourceRow,
                el('div', { class: 'tool-options', id: 'tool-options' }, basic),
                el('details', { class: 'tool-more' }, el('summary', { text: tt('advanced') }), el('div', { class: 'tool-options' }, advanced)),
                el('div', { class: 'tool-actions' }, start)));
        container.replaceChildren(form, el('div', { id: 'tool-run' }), el('div', { id: 'tool-result' }));
        wireDependencies();
    }

    /** Controls that only matter for another choice are hidden (minutes for "every N minutes", parts for "N equal parts"). */
    function wireDependencies() {
        const mode = state.controls.split_mode;
        if (mode) {
            const apply = () => {
                if (state.controls.split_minutes) state.controls.split_minutes.wrap.style.display = mode.read() === 'duration' ? '' : 'none';
                if (state.controls.split_parts) state.controls.split_parts.wrap.style.display = mode.read() === 'parts' ? '' : 'none';
            };
            mode.input.addEventListener('change', apply);
            apply();
        }
    }

    async function pasteInto(input) {
        try { input.value = (await navigator.clipboard.readText()).trim(); }
        catch (e) { toast(tt('pasteFailed')); }
    }

    async function uploadFile(fileInput, target) {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        const body = new FormData();
        body.append('video', file);
        toast(tt('uploading'));
        try {
            const res = await fetch('/api/upload', { method: 'POST', body });
            const data = await res.json();
            if (res.ok && data.path) { target.value = data.path; toast(tt('uploaded', { name: data.name || file.name }), 'success'); }
            else toast(tt('uploadFailed'), 'error');
        } catch (e) { toast(tt('uploadFailed'), 'error'); }
    }

    function collect(input) {
        const tool = state.tool;
        const body = { engine_mode: TOOLS[tool].engine, input };
        for (const [name, control] of Object.entries(state.controls)) {
            if (control.wrap && control.wrap.style.display === 'none') continue;     // a hidden control is not part of the request
            body[name] = control.read();
        }
        return body;
    }

    async function startRun(input, button) {
        if (!input) { toast(tt('needInput'), 'error'); return; }
        const body = collect(input);
        if (body.clip_min_sec !== undefined && body.clip_max_sec !== undefined && body.clip_min_sec > body.clip_max_sec) {
            toast(tt('needRange'), 'error');
            return;
        }
        button.disabled = true;
        button.textContent = tt('starting');
        try {
            const data = await api('/api/start', { method: 'POST', body: JSON.stringify(body) });
            state.jobId = data.job_id;
            state.view = 'run';
            showRun(data);
        } catch (e) {
            toast(tt('startFailed') + ': ' + e.message, 'error');
            button.disabled = false;
            button.textContent = tt('start');
        }
    }

    // ── run monitor ────────────────────────────────────────────────────────────────
    function showRun(first) {
        const form = $('tool-form');
        if (form) form.classList.add('is-running');
        const run = $('tool-run');
        const bar = el('div', { class: 'tool-bar-fill', id: 'tool-bar-fill', style: 'width:3%;' });
        const phase = el('div', { class: 'tool-phase', id: 'tool-phase', text: first && first.status === 'queued' ? tt('queued', { n: first.position || 1 }) : tt('running') });
        const meta = el('div', { class: 'tool-run-meta', id: 'tool-run-meta' });
        const log = el('pre', { class: 'tool-log', id: 'tool-log', 'aria-live': 'polite' });
        const stop = el('button', { type: 'button', class: 'btn-danger btn-sm', id: 'tool-stop', text: tt('stop'), onclick: () => stopRun(stop) });
        run.replaceChildren(el('div', { class: 'card tool-run-card' },
            el('div', { class: 'row-between' }, phase, stop),
            el('div', { class: 'tool-bar' }, bar), meta, log));
        stopPolling();
        state.timer = setInterval(poll, 1500);
        poll();
    }

    async function stopRun(button) {
        if (!state.jobId) return;
        button.disabled = true;
        try { await api('/api/cancel/' + encodeURIComponent(state.jobId), { method: 'POST' }); }
        catch (e) { toast(e.message, 'error'); button.disabled = false; }
    }

    async function poll() {
        const jobId = state.jobId;
        if (!jobId) return;
        let data;
        try { data = await api('/api/status/' + encodeURIComponent(jobId)); }
        catch (e) { return; }                                  // a missed poll is not an error
        if (jobId !== state.jobId) return;                     // another run was started meanwhile
        const bar = $('tool-bar-fill'), phase = $('tool-phase'), meta = $('tool-run-meta'), log = $('tool-log');
        if (!bar) return;
        const status = data.status;
        bar.style.width = Math.max(3, Math.min(100, data.progress || 0)) + '%';
        phase.textContent = data.phase || (status === 'queued' ? tt('queued', { n: '?' }) : tt('running'));
        meta.textContent = [data.elapsed_sec !== null && data.elapsed_sec !== undefined ? tt('elapsed', { time: clock(data.elapsed_sec) }) : '', data.batch_status || ''].filter(Boolean).join('   |   ');
        log.textContent = (data.log || []).slice(-8).join('\n');
        log.scrollTop = log.scrollHeight;
        if (status === 'running' || status === 'queued') return;

        stopPolling();
        const stop = $('tool-stop');
        if (stop) stop.remove();
        if (status === 'done') {
            bar.style.width = '100%';
            bar.classList.add('is-done');
            phase.textContent = tt('done');
            await showResult(data.project_dir, { packageUrl: data.package_url, zipUrl: data.zip_url, after: true });
        } else {
            bar.classList.add(status === 'cancelled' ? 'is-warn' : 'is-error');
            phase.textContent = status === 'cancelled' ? tt('cancelled') : tt('failed') + ': ' + (data.error || data.phase || '');
            const form = $('tool-form');
            if (form) form.classList.remove('is-running');
            const start = $('tool-start');
            if (start) { start.disabled = false; start.textContent = tt('again'); }
        }
    }

    // ── result viewer ──────────────────────────────────────────────────────────────
    async function showResult(projectName, options) {
        const target = $('tool-result');
        if (!target || !projectName) return;
        try {
            const data = await api('/api/projects/' + encodeURIComponent(projectName) + '/tool-result');
            target.replaceChildren(renderResult(data, options || {}));
            const form = $('tool-form');
            if (form) form.classList.remove('is-running');
            const start = $('tool-start');
            if (start) { start.disabled = false; start.textContent = tt('newRun'); }
        } catch (e) {
            target.replaceChildren(el('div', { class: 'empty-state' }, tt('resultFailed') + ': ' + e.message));
        }
    }

    /** The result of a project opened from the Projects view (no form, just the result). */
    async function openResult(projectName) {
        await loadModel();
        stopPolling();
        state.tool = null;
        state.view = 'result';
        window.switchView('tool');
        const titleEl = $('header-title-text');
        if (titleEl) titleEl.textContent = tt('title_result');
        const container = root();
        container.replaceChildren(el('div', { class: 'card tool-panel' }, header('title_result', null)), el('div', { id: 'tool-result' }));
        await showResult(projectName, {});
    }

    function link(label, href, props) {
        return el('a', Object.assign({ class: 'btn-secondary btn-sm', href, text: label, rel: 'noopener' }, props || {}));
    }

    async function copyText(text, button) {
        try {
            await navigator.clipboard.writeText(text);
            const old = button.textContent;
            button.textContent = tt('copied');
            setTimeout(() => { button.textContent = old; }, 1400);
        } catch (e) { toast(text); }
    }

    function scoreTone(score) { return score >= 80 ? 'chip-ok' : (score >= 55 ? 'chip-gold' : 'chip-warn'); }

    function renderResult(data, options) {
        const items = data.items || [];
        const isShorts = data.engine_type === 'shorts';
        const heading = el('div', { class: 'tool-result-head' },
            el('div', {},
                el('div', { class: 'card-title', text: isShorts ? tt('resultShorts', { n: items.length }) : tt('resultParts', { n: items.length }) }),
                el('div', { class: 'card-desc', text: tt('source_label') + ': ' + (data.source || '') + (data.duration ? '  |  ' + clock(data.duration) : '') })),
            el('div', { class: 'tool-result-actions' },
                data.package_url ? link(tt('capcutPackage'), data.package_url, { class: 'btn-primary btn-sm', id: 'tool-package' }) : null,
                data.captions_url ? link(tt('captionsList'), data.captions_url, { target: '_blank' }) : null,
                link(tt('zipAll'), data.zip_url, { id: 'tool-zip' })));
        if (!items.length) return el('div', { class: 'card' }, heading, el('div', { class: 'empty-state', text: tt('resultEmpty') }));
        const grid = el('div', { class: isShorts ? 'short-grid' : 'part-list' }, items.map(item => isShorts ? shortCard(item) : partRow(item)));
        return el('div', { class: 'card tool-result-card' }, heading, grid);
    }

    function shortCard(item) {
        const main = (item.videos || [])[0];
        const playUrl = main ? main.url : item.clean_url;
        const cover = item.cover_url
            ? el('img', { class: 'short-cover', src: item.cover_url, alt: item.title || tt('cover'), loading: 'lazy' })
            : el('div', { class: 'short-cover short-cover-empty' });
        const thumb = el('button', { type: 'button', class: 'short-thumb', 'aria-label': tt('play'), disabled: !playUrl,
            onclick: () => { if (playUrl && typeof window.openVideoPlayerModal === 'function') window.openVideoPlayerModal(playUrl, item.title || ''); } },
            cover, el('span', { class: 'short-play' }, svg('M8 5v14l11-7z')),
            el('span', { class: 'short-time', text: clock(item.start) + ' - ' + clock(item.end) }));
        const links = el('div', { class: 'short-links' },
            ...(item.videos || []).map(v => link(tt('download') + (item.videos.length > 1 ? ' ' + v.format : ''), v.url, { download: v.name })),
            item.clean_url ? link(tt('cleanClip'), item.clean_url, { download: 'clip_' + String(item.index).padStart(2, '0') + '.mp4' }) : null,
            item.srt_url ? link(tt('subtitlesSrt'), item.srt_url, { download: 'short_' + String(item.index).padStart(2, '0') + '.srt' }) : null);
        const tags = (item.hashtags || []).length ? el('div', { class: 'short-tags' }, item.hashtags.map(tag => el('span', { class: 'chip', text: tag }))) : null;
        const copyRow = el('div', { class: 'short-copy' },
            item.caption ? el('button', { type: 'button', class: 'btn-nav btn-sm', text: tt('copyCaption'), onclick: event => copyText(item.caption, event.currentTarget) }) : null,
            tags ? el('button', { type: 'button', class: 'btn-nav btn-sm', text: tt('copyTags'), onclick: event => copyText(item.hashtags.join(' '), event.currentTarget) }) : null);
        return el('article', { class: 'short-card', 'data-index': item.index },
            thumb,
            el('div', { class: 'short-body' },
                el('div', { class: 'row-between' }, el('div', { class: 'short-title', text: item.title || ('#' + item.index) }),
                    el('span', { class: 'chip ' + scoreTone(item.score), text: tt('score', { n: Math.round(item.score) }) })),
                item.caption ? el('p', { class: 'short-caption', text: item.caption }) : null,
                tags, item.reason ? el('p', { class: 'short-reason', title: tt('why'), text: item.reason }) : null,
                copyRow, links));
    }

    function partRow(item) {
        return el('div', { class: 'part-row', 'data-index': item.index },
            el('span', { class: 'part-no', text: tt('part', { n: item.index }) }),
            el('span', { class: 'part-time', text: clock(item.start) + ' - ' + clock(item.end) }),
            el('span', { class: 'muted', text: clock(item.duration) + '   |   ' + tt('size', { mb: Number(item.size_mb || 0).toFixed(1) }) }),
            el('span', { class: 'part-actions' },
                el('button', { type: 'button', class: 'btn-nav btn-sm', text: tt('play'),
                    onclick: () => { if (typeof window.openVideoPlayerModal === 'function') window.openVideoPlayerModal(item.url, tt('part', { n: item.index })); } }),
                link(tt('download'), item.url, { download: item.file })));
    }

    // ── boot ──────────────────────────────────────────────────────────────────────
    function init() {
        renderHub();
        document.addEventListener('pai:language', () => {
            renderHub();
            if (state.view === 'form' && state.tool && $('view-tool') && $('view-tool').classList.contains('active')) renderForm();
        });
    }

    window.PaiTools = { openTool, openResult, startWizard, renderHub, tt, TEXT, TOOLS };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
