/* ==========================================================================
   Pai AI Movie Studio - studio views
   ==========================================================================
   Everything the dashboard needs beyond the production wizard (which lives in index.html):

     - theme toggle (light / dark), storage meter in the sidebar
     - Projects: library with quality score, job history
     - Script Editor: fix narration lines, re-render only what changed
     - Brand Kit: channel profiles (save / apply the wizard settings)

   It talks to the studio API (services/studio_api.py) and reuses a few helpers of the inline script
   (showToast, switchView, goToStep, startStreamingLogs, OPTION_READERS ...). All text goes through
   textContent / DOM nodes - nothing from the server is ever injected as HTML.
   ========================================================================== */
(function () {
    'use strict';

    // ── helpers ───────────────────────────────────────────────────────────────
    const $ = id => document.getElementById(id);
    const PER_VIDEO_OPTIONS = ['engine_mode', 'input', 'project_name', 'custom_thumb_title', 'trim_end',
        'no_smart_trim', 'context_hint', 'resume', 'force_whisper'];       // mirrors services/profiles.py

    function el(tag, props, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(props || {})) {
            if (value === undefined || value === null || value === false) continue;
            if (key === 'class') node.className = value;
            else if (key === 'text') node.textContent = value;
            else if (key === 'style') node.setAttribute('style', value);
            else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
            else node.setAttribute(key, value === true ? '' : value);
        }
        for (const child of children.flat()) {
            if (child === null || child === undefined || child === false) continue;
            node.append(child instanceof Node ? child : document.createTextNode(String(child)));
        }
        return node;
    }

    function toast(message, type) {
        if (typeof window.showToast === 'function') window.showToast(message, type || 'info');
    }

    async function api(path, options) {
        const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {}));
        let body = null;
        try { body = await res.json(); } catch (e) { /* empty / non-JSON body */ }
        if (!res.ok) {
            const detail = body && (body.detail || body.error);
            const error = new Error(typeof detail === 'string' ? detail : (res.status + ' ' + res.statusText));
            error.status = res.status;
            throw error;
        }
        return body;
    }

    function formatDate(value) {
        if (value === null || value === undefined || value === '') return '-';
        const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
        if (Number.isNaN(date.getTime())) return '-';
        return date.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    }

    function clock(seconds) {
        const total = Math.max(0, Math.floor(Number(seconds) || 0));
        const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
        const mm = String(m).padStart(2, '0'), ss = String(s).padStart(2, '0');
        return h ? h + ':' + mm + ':' + ss : mm + ':' + ss;
    }

    const lang = () => {
        try { return (typeof currentLang !== 'undefined' && currentLang) || localStorage.getItem('pai_ai_lang') || 'my'; }
        catch (e) { return 'my'; }
    };


    // ── i18n of the studio views (the wizard keeps its own dictionary in index.html) ──
    const I18N = {
        en: {
            navWorkspace: 'STUDIO WORKSPACE', navSystem: 'SYSTEM & UTILITIES', navProjects: 'Projects', navBrand: 'Brand Kit',
            storageUsed: 'Disk used', back: '← Projects', close: 'Close', refresh: 'Refresh', loading: 'Loading...',
            titleProjects: 'Projects', titleEditor: 'Script Editor', titleBrand: 'Brand Kit',
            pjTitle: 'Projects', pjSub: 'Every finished job: review its quality, fix the script and re-render only what changed.',
            pjTabProjects: 'Projects', pjTabHistory: 'Job history',
            histJob: 'Job', histStatus: 'Status', histPhase: 'Last phase', histStarted: 'Started', histEmpty: 'No jobs yet.',
            statProjects: 'Projects', statReview: 'Lines to review', statReady: 'Ready to publish', statStorage: 'Storage',
            pjEmptyTitle: 'No projects yet', pjEmptyText: 'Run a production in the Production Studio and it will appear here.',
            pjEdit: 'Edit script', pjQuality: 'Quality', pjRerender: 'Re-render', pjFiles: 'Files',
            pjLines: '{n} lines', pjToReview: '{n} to review', pjClean: 'Clean', pjUpdated: 'Updated {when}',
            verdictReady: 'Ready', verdictReview: 'Review', verdictAttention: 'Needs attention', verdictUnknown: 'Not scored',
            seSub: 'Fix a line, then re-render: only the lines you changed are re-voiced.',
            seSearch: 'Search narration or source...', seAll: 'All', seReview: 'Needs review', seOver: 'Too long', seEdited: 'Edited',
            seNoChanges: 'No changes', seDirty: '{n} unsaved', seSave: 'Save', seSaveRender: 'Save & re-render',
            seSaved: 'Saved {n} change(s).', seNothing: 'Nothing to save.', seEmpty: 'No lines match.',
            seSource: 'Source', seMarkChecked: 'Mark as checked', seMale: 'Male voice', seFemale: 'Female voice',
            seRerenderStarted: 'Re-render started: only the changed lines are re-voiced.', seNotEditable: 'This project has no editable script.',
            seLoadFailed: 'Could not load the script', seSaveFailed: 'Could not save', seUnsaved: 'You have unsaved changes. Leave anyway?',
            seTooLong: 'Longer than the time slot allows', seFlagged: 'Needs a human look',
            qTitle: 'Quality report', qScript: 'Script', qVoice: 'Voice-over', qLines: 'Lines', qNeedsReview: 'Need review', qOverBudget: 'Too long',
            qEdited: 'Edited by you', qClips: 'Voice clips', qMissing: 'Missing clips', qOverflow: 'Clips longer than their slot',
            qReviewLines: 'Lines that need a look', qWarnings: 'Warnings', qErrors: 'Errors', qNone: 'Nothing to report.',
            qVerdictReady: 'Ready to publish.', qVerdictReview: 'Almost there - a few lines deserve a look.',
            qVerdictAttention: 'Needs attention before publishing.', qVideo: 'Video', qStatus: 'Status', qDuration: 'Run time',
            qSubtitles: 'Subtitles', qFailed: 'Could not build the report',
            bkTitle: 'Brand Kit', bkSub: "Save a channel's look and voice once; apply it to any new production in one click.",
            bkSaveTitle: 'Save the current wizard settings',
            bkSaveDesc: 'Voice, subtitle style, watermark, protection and audio choices are stored. The video, thumbnail title and story hint are per-video and never saved.',
            bkName: 'Profile name', bkNamePh: 'e.g. My Recap Channel', bkSaveBtn: 'Save profile',
            bkApply: 'Apply to wizard', bkDelete: 'Delete', bkEmptyTitle: 'No profiles yet',
            bkEmptyText: 'Set up the wizard the way your channel looks, then save it here.', bkSaved: 'Profile saved.',
            bkApplied: 'Applied "{name}" to the wizard.', bkDeleted: 'Profile deleted.', bkNeedName: 'Give the profile a name first.',
            bkConfirmDelete: 'Delete profile "{name}"?', bkFailed: 'Profile error',
            engineRecap: 'Recap', engineSubtitle: 'Subtitle', engineHardsub: 'Hardsub',
            statusRunning: 'running', statusDone: 'done', statusError: 'error', statusCancelled: 'cancelled', statusQueued: 'queued',
            COMPLETED: 'Completed', COMPLETED_WITH_WARNINGS: 'Completed with warnings', FAILED: 'Failed', CANCELLED: 'Cancelled',
            RUNNING: 'Running', QA_PENDING: 'QA pending', QUEUED: 'Queued',
            issue_missing: 'No translation', issue_placeholder: 'Empty text', issue_meta_text: 'Model chatter',
            issue_untranslated: 'Not translated', issue_foreign_script: 'Foreign script', issue_latin_words: 'English words left',
            issue_copied_source: 'Copied from source', issue_duplicate: 'Repeats previous line', issue_repeat_boundary: 'Restates previous line',
            keyTest: 'Test keys', keyChecking: 'Checking...', keyOnline: 'works', keyInvalid: 'invalid key', keyRate: 'valid, rate limited',
            keyUnreachable: 'could not reach Google', keyNoKeys: 'Paste at least one key first.',
            keyAllGood: 'All {n} key(s) work. Press Save Keys to keep them.', keySomeBad: '{bad} of {n} key(s) do not work.', keyFailed: 'Could not test the keys',
            dictTitle: 'Pronunciation dictionary', dictDesc: 'Fix names and loanwords the voice mispronounces. Only the spoken text changes; the script and subtitles keep the original spelling.',
            dictLabel: 'One entry per line: original => how it should be read', dictHint: 'Latin words match as whole words, ignoring case. Burmese terms match anywhere in a line.',
            dictSave: 'Save dictionary', dictSaved: 'Saved {n} entries. Lines that contain them are re-voiced on the next run.', dictFailed: 'Could not save the dictionary',
            stTitle: 'Story bible & rename', stEmpty: 'No story bible was built for this project (a short clip, or it was made before story memory).',
            stSummary: 'Summary', stTone: 'Tone', stCharacters: 'Characters', stPlaces: 'Places', stTerms: 'Terms', stRelations: 'Relationships',
            stClickHint: 'Click a name to rename it everywhere in the script.',
            rpFind: 'Find', rpWith: 'Replace with', rpMatchCase: 'Match case', rpWholeWord: 'Whole word (Latin)', rpPreview: 'Preview', rpApply: 'Replace all',
            rpNone: 'No line contains it.', rpFound: '{lines} line(s), {n} occurrence(s) found.', rpDone: 'Replaced in {lines} line(s). Press Save & re-render to hear it.',
            rpConfirm: 'Replace in {lines} line(s)?', rpSaveFirst: 'Save your edits first, then replace.', rpNeedFind: 'Enter the text to find.', rpFailed: 'Replace failed',
            optStoryLength: 'Story length (Recap style)', optLenStandard: 'Standard', optLenCompact: 'Compact - plot-critical only', optLenDetailed: 'Detailed - actions and small beats', optLenScene: 'Scene by scene - longest',
            optNarrativeTone: 'Narrative tone (Recap style)', optToneNatural: 'Natural', optToneBrisk: 'Brisk - energetic', optToneCalm: 'Calm - reflective', optToneDramatic: 'Dramatic - suspenseful',
            optTtsRate: 'Narration speed', optRateDefault: 'Default (from config)', optRateSlow: 'Slow (-15%)', optRateNormal: 'Normal (0%)', optRateFast: 'Fast (+15%)', optRateVeryFast: 'Very fast (+30%)',
            pjCapcut: 'CapCut', capBuilding: 'Building the CapCut package...', capReady: 'CapCut package ready ({mb} MB) - downloading.',
            capFailed: 'Could not build the CapCut package',
            optDelivery: 'How should it be delivered?', optDeliveryOneclick: 'One-click finished video',
            optDeliveryOneclickDesc: 'Everything start to end: you get the finished video, rendered for you.',
            optDeliveryCapcut: 'CapCut pipeline',
            optDeliveryCapcutDesc: 'Skips the slow final render. You get a package to finish in CapCut: narration track, Burmese-safe captions and a guide.',
            optDeliveryBoth: 'Both', optDeliveryBothDesc: 'The finished video and the CapCut package.',
            optDeliveryNoteCapcut: 'No final video is rendered in this mode, so the Deliverable setting below is ignored. The run ends with capcut_package.zip.',
            optDeliveryNoteBoth: 'Takes as long as the finished video, then adds capcut_package.zip.',
            revDelivery: 'Delivery: {mode}', dlCapcut: 'Download CapCut package', capDoneToast: 'CapCut package ready - downloading.',
            engineShorts: 'Shorts', engineSplitter: 'Splitter', pjClips: '{n} Shorts', pjParts: '{n} parts', pjResults: 'Results',
            issue_over_budget: 'Too long'
        },
        my: {
            navWorkspace: 'စတူဒီယို လုပ်ငန်းခွင်', navSystem: 'စနစ်နှင့် ကိရိယာများ', navProjects: 'ပရောဂျက်များ', navBrand: 'Brand Kit',
            storageUsed: 'သုံးစွဲထားသော နေရာ', back: '← ပရောဂျက်များ', close: 'ပိတ်မည်', refresh: 'ပြန်လည်ရယူ', loading: 'ခဏစောင့်ပါ...',
            titleProjects: 'ပရောဂျက်များ', titleEditor: 'ဇာတ်ညွှန်း တည်းဖြတ်ရန်', titleBrand: 'Brand Kit (ချန်နယ် ပုံစံ)',
            pjTitle: 'ပရောဂျက်များ', pjSub: 'ပြီးစီးထားသော အလုပ်တိုင်း၏ အရည်အသွေးကို စစ်ဆေး၊ ဇာတ်ညွှန်းကို ပြင်၊ ပြောင်းထားသည့် စာကြောင်းများကိုသာ ပြန်ထုတ်နိုင်သည်။',
            pjTabProjects: 'ပရောဂျက်များ', pjTabHistory: 'အလုပ်မှတ်တမ်း',
            histJob: 'အလုပ်', histStatus: 'အခြေအနေ', histPhase: 'နောက်ဆုံး အဆင့်', histStarted: 'စတင်ချိန်', histEmpty: 'အလုပ်မှတ်တမ်း မရှိသေးပါ။',
            statProjects: 'ပရောဂျက်', statReview: 'စစ်ရန် စာကြောင်း', statReady: 'တင်ရန် အဆင်သင့်', statStorage: 'နေရာသုံးစွဲမှု',
            pjEmptyTitle: 'ပရောဂျက် မရှိသေးပါ', pjEmptyText: 'Production Studio တွင် အလုပ်တစ်ခု လုပ်ဆောင်လိုက်ပါက ဤနေရာတွင် ပေါ်လာပါမည်။',
            pjEdit: 'ဇာတ်ညွှန်း ပြင်မည်', pjQuality: 'အရည်အသွေး', pjRerender: 'ပြန်ထုတ်မည်', pjFiles: 'ဖိုင်များ',
            pjLines: '{n} ကြောင်း', pjToReview: 'စစ်ရန် {n}', pjClean: 'ရှင်းလင်း', pjUpdated: '{when} တွင် ပြင်ထားသည်',
            verdictReady: 'အဆင်သင့်', verdictReview: 'ပြန်စစ်ရန်', verdictAttention: 'ဂရုစိုက်ရန်', verdictUnknown: 'အမှတ်မပေးရသေး',
            seSub: 'စာကြောင်းကို ပြင်ပြီး ပြန်ထုတ်ပါ - ပြင်ထားသော စာကြောင်းများကိုသာ အသံပြန်ထုတ်ပါမည်။',
            seSearch: 'ဇာတ်ညွှန်း သို့မဟုတ် မူရင်းစာ ရှာရန်...', seAll: 'အားလုံး', seReview: 'စစ်ရန်', seOver: 'ရှည်လွန်း', seEdited: 'ပြင်ထားသည်',
            seNoChanges: 'ပြောင်းလဲမှု မရှိ', seDirty: 'မသိမ်းရသေး {n}', seSave: 'သိမ်းမည်', seSaveRender: 'သိမ်း၍ ပြန်ထုတ်မည်',
            seSaved: 'ပြောင်းလဲမှု {n} ခု သိမ်းပြီးပါပြီ။', seNothing: 'သိမ်းစရာ မရှိပါ။', seEmpty: 'ကိုက်ညီသော စာကြောင်း မရှိပါ။',
            seSource: 'မူရင်း', seMarkChecked: 'စစ်ပြီးပြီ', seMale: 'အမျိုးသား အသံ', seFemale: 'အမျိုးသမီး အသံ',
            seRerenderStarted: 'ပြန်ထုတ်ခြင်း စတင်ပါပြီ - ပြင်ထားသော စာကြောင်းများကိုသာ အသံပြန်ထုတ်ပါမည်။', seNotEditable: 'ဤပရောဂျက်တွင် ပြင်နိုင်သော ဇာတ်ညွှန်း မရှိပါ။',
            seLoadFailed: 'ဇာတ်ညွှန်းကို မဖတ်နိုင်ပါ', seSaveFailed: 'မသိမ်းနိုင်ပါ', seUnsaved: 'မသိမ်းရသေးသော ပြောင်းလဲမှုများ ရှိပါသည်။ ထွက်မလား?',
            seTooLong: 'အချိန်ပေးထားသည်ထက် ရှည်နေသည်', seFlagged: 'လူကိုယ်တိုင် စစ်သင့်သည်',
            qTitle: 'အရည်အသွေး အစီရင်ခံစာ', qScript: 'ဇာတ်ညွှန်း', qVoice: 'အသံထွက်', qLines: 'စာကြောင်းများ', qNeedsReview: 'စစ်ရန်', qOverBudget: 'ရှည်လွန်း',
            qEdited: 'သင်ပြင်ထားသည်', qClips: 'အသံ clip များ', qMissing: 'ပျောက်နေသော clip', qOverflow: 'အချိန်ထက်ရှည်သော clip',
            qReviewLines: 'ကြည့်သင့်သော စာကြောင်းများ', qWarnings: 'သတိပေးချက်များ', qErrors: 'အမှားများ', qNone: 'ဖော်ပြစရာ မရှိပါ။',
            qVerdictReady: 'တင်ရန် အဆင်သင့်ဖြစ်ပါပြီ။', qVerdictReview: 'နီးပါပြီ - စာကြောင်း အနည်းငယ်ကို ပြန်ကြည့်သင့်သည်။',
            qVerdictAttention: 'မတင်မီ ပြင်ဆင်ရန် လိုအပ်သည်။', qVideo: 'ဗီဒီယို', qStatus: 'အခြေအနေ', qDuration: 'ကြာချိန်',
            qSubtitles: 'စာတန်းထိုး', qFailed: 'အစီရင်ခံစာ မဖန်တီးနိုင်ပါ',
            bkTitle: 'Brand Kit (ချန်နယ် ပုံစံ)', bkSub: 'ချန်နယ်တစ်ခု၏ အသွင်အပြင်နှင့် အသံကို တစ်ကြိမ်သိမ်းထားပြီး နောက်ထုတ်လုပ်မှုတိုင်းတွင် တစ်ချက်နှိပ်ရုံ သုံးနိုင်သည်။',
            bkSaveTitle: 'လက်ရှိ wizard ရွေးချယ်မှုများကို သိမ်းမည်',
            bkSaveDesc: 'အသံ၊ စာတန်းပုံစံ၊ watermark၊ ကာကွယ်မှုနှင့် အသံ ရွေးချယ်မှုများကို သိမ်းပါမည်။ ဗီဒီယို၊ thumbnail ခေါင်းစဉ်နှင့် ဇာတ်လမ်းအညွှန်းတို့သည် ဗီဒီယိုတစ်ခုချင်းစီနှင့် သက်ဆိုင်သဖြင့် မသိမ်းပါ။',
            bkName: 'Profile အမည်', bkNamePh: 'ဥပမာ - ကျွန်ုပ်၏ Recap ချန်နယ်', bkSaveBtn: 'Profile သိမ်းမည်',
            bkApply: 'Wizard တွင် သုံးမည်', bkDelete: 'ဖျက်မည်', bkEmptyTitle: 'Profile မရှိသေးပါ',
            bkEmptyText: 'ချန်နယ်၏ ပုံစံအတိုင်း wizard ကို သတ်မှတ်ပြီး ဤနေရာတွင် သိမ်းထားပါ။', bkSaved: 'Profile သိမ်းပြီးပါပြီ။',
            bkApplied: '"{name}" ကို wizard တွင် ထည့်သွင်းပြီးပါပြီ။', bkDeleted: 'Profile ဖျက်ပြီးပါပြီ။', bkNeedName: 'Profile အမည်ကို အရင်ပေးပါ။',
            bkConfirmDelete: '"{name}" profile ကို ဖျက်မလား?', bkFailed: 'Profile အမှား',
            engineRecap: 'Recap', engineSubtitle: 'စာတန်း', engineHardsub: 'Hardsub',
            statusRunning: 'လုပ်ဆောင်နေ', statusDone: 'ပြီးစီး', statusError: 'အမှား', statusCancelled: 'ရပ်ထား', statusQueued: 'တန်းစီနေ',
            COMPLETED: 'ပြီးစီးပါပြီ', COMPLETED_WITH_WARNINGS: 'သတိပေးချက်များဖြင့် ပြီးစီး', FAILED: 'မအောင်မြင်ပါ', CANCELLED: 'ရပ်တန့်ထားသည်',
            RUNNING: 'လုပ်ဆောင်နေသည်', QA_PENDING: 'QA စောင့်နေသည်', QUEUED: 'တန်းစီနေသည်',
            issue_missing: 'ဘာသာပြန် မရှိ', issue_placeholder: 'စာသား အလွတ်', issue_meta_text: 'မလိုသော ရှင်းလင်းချက်',
            issue_untranslated: 'မဘာသာပြန်ရသေး', issue_foreign_script: 'ပြည်ပစာလုံး ပါနေ', issue_latin_words: 'အင်္ဂလိပ်စာလုံး ကျန်နေ',
            issue_copied_source: 'မူရင်းကို ကူးထား', issue_duplicate: 'ရှေ့စာကြောင်းနှင့် ထပ်နေ', issue_repeat_boundary: 'ရှေ့စာကြောင်းကို ပြန်ပြောနေ',
            keyTest: 'Key စမ်းစစ်မည်', keyChecking: 'စစ်ဆေးနေသည်...', keyOnline: 'အသုံးပြုနိုင်သည်', keyInvalid: 'Key မမှန်ပါ', keyRate: 'မှန်သည်၊ ခေတ္တ limit ပြည့်နေ',
            keyUnreachable: 'Google သို့ မချိတ်ဆက်နိုင်ပါ', keyNoKeys: 'Key တစ်ခုခု အရင်ထည့်ပါ။',
            keyAllGood: 'Key {n} ခုလုံး အသုံးပြုနိုင်ပါသည်။ သိမ်းရန် Save Keys ကို နှိပ်ပါ။', keySomeBad: 'Key {n} ခုအနက် {bad} ခု အလုပ်မလုပ်ပါ။', keyFailed: 'Key များကို မစစ်နိုင်ပါ',
            dictTitle: 'အသံထွက် အဘိဓာန်', dictDesc: 'အသံက မှားဖတ်တတ်သော နာမည်နှင့် ပြင်ပစကားလုံးများကို ပြင်ပါ။ အသံထွက်မည့် စာသားသာ ပြောင်းပြီး ဇာတ်ညွှန်းနှင့် စာတန်းထိုးတို့တွင် မူလစာလုံးပေါင်း ကျန်ရှိပါမည်။',
            dictLabel: 'တစ်ကြောင်းလျှင် တစ်ခု - မူရင်း => ဖတ်စေလိုသည့်ပုံစံ', dictHint: 'အင်္ဂလိပ်စာလုံးများကို စာလုံးအကြီးအသေး မခွဲဘဲ စကားလုံးတစ်ခုလုံးအဖြစ် တိုက်ဆိုင်ပါသည်။ မြန်မာစာလုံးများကို စာကြောင်းအတွင်း နေရာမရွေး တိုက်ဆိုင်ပါသည်။',
            dictSave: 'အဘိဓာန် သိမ်းမည်', dictSaved: 'အဘိဓာန် {n} ခု သိမ်းပြီးပါပြီ။ ပါဝင်သော စာကြောင်းများကို နောက်တစ်ကြိမ် run တွင် အသံပြန်ထုတ်ပါမည်။', dictFailed: 'အဘိဓာန်ကို မသိမ်းနိုင်ပါ',
            stTitle: 'ဇာတ်လမ်းအချက်အလက် နှင့် နာမည်ပြောင်းရန်', stEmpty: 'ဤပရောဂျက်အတွက် ဇာတ်လမ်းအချက်အလက် မတည်ဆောက်ထားပါ (clip တိုခြင်း သို့မဟုတ် story memory မတိုင်မီ ထုတ်ထားခြင်း)။',
            stSummary: 'အကျဉ်းချုပ်', stTone: 'လေသံ', stCharacters: 'ဇာတ်ကောင်များ', stPlaces: 'နေရာများ', stTerms: 'စကားလုံးများ', stRelations: 'ဆက်ဆံရေးများ',
            stClickHint: 'နာမည်ကို နှိပ်ပါ - ဇာတ်ညွှန်းတစ်ခုလုံးတွင် ပြောင်းနိုင်ပါသည်။',
            rpFind: 'ရှာရန်', rpWith: 'အစားထိုးမည့်စာ', rpMatchCase: 'အကြီးအသေး ခွဲမည်', rpWholeWord: 'စကားလုံးတစ်ခုလုံး (အင်္ဂလိပ်)', rpPreview: 'ကြိုကြည့်မည်', rpApply: 'အားလုံး အစားထိုးမည်',
            rpNone: 'ပါဝင်သော စာကြောင်း မရှိပါ။', rpFound: 'စာကြောင်း {lines} ကြောင်း၊ {n} နေရာ တွေ့ပါသည်။', rpDone: 'စာကြောင်း {lines} ကြောင်းတွင် အစားထိုးပြီးပါပြီ။ ကြားနိုင်ရန် သိမ်း၍ ပြန်ထုတ်မည်ကို နှိပ်ပါ။',
            rpConfirm: 'စာကြောင်း {lines} ကြောင်းတွင် အစားထိုးမလား?', rpSaveFirst: 'ပြင်ထားသည်များကို အရင်သိမ်းပြီးမှ အစားထိုးပါ။', rpNeedFind: 'ရှာလိုသောစာကို ထည့်ပါ။', rpFailed: 'အစားထိုး၍ မရပါ',
            optStoryLength: 'ဇာတ်လမ်း အရှည်အတို (Recap ပုံစံ)', optLenStandard: 'ပုံမှန်', optLenCompact: 'အကျဉ်း - အဓိကဖြစ်ရပ်များသာ', optLenDetailed: 'အသေးစိတ် - လုပ်ရပ်နှင့် အသေးစား အခန်းများပါ', optLenScene: 'ဇာတ်ခန်းတိုင်း - အရှည်ဆုံး',
            optNarrativeTone: 'ပြောပုံ လေသံ (Recap ပုံစံ)', optToneNatural: 'သဘာဝကျ', optToneBrisk: 'သွက်လက် - တက်ကြွ', optToneCalm: 'တည်ငြိမ် - စဉ်းစားဖွယ်', optToneDramatic: 'ပြင်းထန် - စိတ်လှုပ်ရှား',
            optTtsRate: 'ပြောနှုန်း အမြန်နှေး', optRateDefault: 'မူလ (config အတိုင်း)', optRateSlow: 'နှေး (-15%)', optRateNormal: 'ပုံမှန် (0%)', optRateFast: 'မြန် (+15%)', optRateVeryFast: 'အလွန်မြန် (+30%)',
            pjCapcut: 'CapCut', capBuilding: 'CapCut package ဆောက်နေသည်...', capReady: 'CapCut package အဆင်သင့် ({mb} MB) - ဒေါင်းလုဒ်လုပ်နေသည်။',
            capFailed: 'CapCut package မဆောက်နိုင်ပါ',
            optDelivery: 'ရလဒ်ကို ဘယ်လို ထုတ်ပေးမလဲ?', optDeliveryOneclick: 'One-click အပြီးသတ်ဗီဒီယို',
            optDeliveryOneclickDesc: 'အစအဆုံး အလိုအလျောက် လုပ်ပြီး ပြီးသား ဗီဒီယိုကို ထုတ်ပေးမယ်။',
            optDeliveryCapcut: 'CapCut pipeline',
            optDeliveryCapcutDesc: 'နှေးတဲ့ နောက်ဆုံး render ကို ကျော်မယ်။ CapCut မှာ ဆက်လုပ်ဖို့ package (အသံတစ်ဖိုင်၊ မြန်မာစာ မပျက်တဲ့ စာတန်း၊ လမ်းညွှန်) ရမယ်။',
            optDeliveryBoth: 'နှစ်မျိုးလုံး', optDeliveryBothDesc: 'ပြီးသား ဗီဒီယိုရော CapCut package ပါ ရမယ်။',
            optDeliveryNoteCapcut: 'ဒီ mode မှာ နောက်ဆုံးဗီဒီယို မ render ပါဘူး၊ အောက်က Deliverable ရွေးချယ်မှုကို လျစ်လျူရှုပါမယ်။ အဆုံးမှာ capcut_package.zip ရပါမယ်။',
            optDeliveryNoteBoth: 'ပြီးသားဗီဒီယိုအတိုင်း အချိန်ကြာပြီးမှ capcut_package.zip ထပ်ထည့်ပေးပါမယ်။',
            revDelivery: 'ထုတ်ပေးပုံ: {mode}', dlCapcut: 'CapCut package ဒေါင်းလုဒ်', capDoneToast: 'CapCut package အဆင်သင့် - ဒေါင်းလုဒ်လုပ်နေသည်။',
            engineShorts: 'Shorts', engineSplitter: 'Splitter', pjClips: 'Shorts {n} ခု', pjParts: 'အပိုင်း {n} ခု', pjResults: 'ရလဒ်များ',
            issue_over_budget: 'ရှည်လွန်း'
        },
        th: {
            navWorkspace: 'พื้นที่ทำงานสตูดิโอ', navSystem: 'ระบบและเครื่องมือ', navProjects: 'โปรเจกต์', navBrand: 'ชุดแบรนด์',
            storageUsed: 'พื้นที่ที่ใช้', back: '← โปรเจกต์', close: 'ปิด', refresh: 'รีเฟรช', loading: 'กำลังโหลด...',
            titleProjects: 'โปรเจกต์', titleEditor: 'แก้ไขสคริปต์', titleBrand: 'ชุดแบรนด์',
            pjTitle: 'โปรเจกต์', pjSub: 'ทุกงานที่เสร็จแล้ว: ตรวจคุณภาพ แก้สคริปต์ และเรนเดอร์ใหม่เฉพาะส่วนที่เปลี่ยน',
            pjTabProjects: 'โปรเจกต์', pjTabHistory: 'ประวัติงาน',
            histJob: 'งาน', histStatus: 'สถานะ', histPhase: 'ขั้นตอนล่าสุด', histStarted: 'เริ่มเมื่อ', histEmpty: 'ยังไม่มีงาน',
            statProjects: 'โปรเจกต์', statReview: 'บรรทัดที่ต้องตรวจ', statReady: 'พร้อมเผยแพร่', statStorage: 'พื้นที่จัดเก็บ',
            pjEmptyTitle: 'ยังไม่มีโปรเจกต์', pjEmptyText: 'รันงานใน Production Studio แล้วจะแสดงที่นี่',
            pjEdit: 'แก้สคริปต์', pjQuality: 'คุณภาพ', pjRerender: 'เรนเดอร์ใหม่', pjFiles: 'ไฟล์',
            pjLines: '{n} บรรทัด', pjToReview: 'ต้องตรวจ {n}', pjClean: 'เรียบร้อย', pjUpdated: 'อัปเดต {when}',
            verdictReady: 'พร้อม', verdictReview: 'ควรตรวจ', verdictAttention: 'ต้องแก้ไข', verdictUnknown: 'ยังไม่ให้คะแนน',
            seSub: 'แก้บรรทัดแล้วเรนเดอร์ใหม่ จะพากย์ใหม่เฉพาะบรรทัดที่แก้',
            seSearch: 'ค้นหาบทบรรยายหรือต้นฉบับ...', seAll: 'ทั้งหมด', seReview: 'ต้องตรวจ', seOver: 'ยาวเกิน', seEdited: 'แก้แล้ว',
            seNoChanges: 'ไม่มีการเปลี่ยนแปลง', seDirty: 'ยังไม่บันทึก {n}', seSave: 'บันทึก', seSaveRender: 'บันทึกและเรนเดอร์ใหม่',
            seSaved: 'บันทึก {n} การเปลี่ยนแปลงแล้ว', seNothing: 'ไม่มีอะไรให้บันทึก', seEmpty: 'ไม่พบบรรทัดที่ตรงกัน',
            seSource: 'ต้นฉบับ', seMarkChecked: 'ตรวจแล้ว', seMale: 'เสียงชาย', seFemale: 'เสียงหญิง',
            seRerenderStarted: 'เริ่มเรนเดอร์ใหม่แล้ว จะพากย์ใหม่เฉพาะบรรทัดที่แก้', seNotEditable: 'โปรเจกต์นี้ไม่มีสคริปต์ที่แก้ไขได้',
            seLoadFailed: 'โหลดสคริปต์ไม่ได้', seSaveFailed: 'บันทึกไม่ได้', seUnsaved: 'มีการเปลี่ยนแปลงที่ยังไม่บันทึก ออกเลยหรือไม่?',
            seTooLong: 'ยาวกว่าเวลาที่กำหนด', seFlagged: 'ควรให้คนตรวจ',
            qTitle: 'รายงานคุณภาพ', qScript: 'สคริปต์', qVoice: 'เสียงพากย์', qLines: 'บรรทัด', qNeedsReview: 'ต้องตรวจ', qOverBudget: 'ยาวเกิน',
            qEdited: 'คุณแก้แล้ว', qClips: 'คลิปเสียง', qMissing: 'คลิปที่หายไป', qOverflow: 'คลิปที่ยาวกว่าช่วงเวลา',
            qReviewLines: 'บรรทัดที่ควรดู', qWarnings: 'คำเตือน', qErrors: 'ข้อผิดพลาด', qNone: 'ไม่มีอะไรต้องรายงาน',
            qVerdictReady: 'พร้อมเผยแพร่', qVerdictReview: 'เกือบเสร็จ มีบางบรรทัดที่ควรดู', qVerdictAttention: 'ต้องแก้ไขก่อนเผยแพร่',
            qVideo: 'วิดีโอ', qStatus: 'สถานะ', qDuration: 'เวลาประมวลผล', qSubtitles: 'คำบรรยาย', qFailed: 'สร้างรายงานไม่ได้',
            bkTitle: 'ชุดแบรนด์', bkSub: 'บันทึกลุคและเสียงของช่องไว้ครั้งเดียว แล้วใช้กับงานใหม่ได้ในคลิกเดียว',
            bkSaveTitle: 'บันทึกการตั้งค่า wizard ปัจจุบัน',
            bkSaveDesc: 'บันทึกเสียง สไตล์คำบรรยาย ลายน้ำ การป้องกัน และเสียง ส่วนวิดีโอ ชื่อ thumbnail และคำใบ้เรื่องเป็นของแต่ละวิดีโอ จึงไม่บันทึก',
            bkName: 'ชื่อโปรไฟล์', bkNamePh: 'เช่น ช่องรีแคปของฉัน', bkSaveBtn: 'บันทึกโปรไฟล์',
            bkApply: 'ใช้กับ wizard', bkDelete: 'ลบ', bkEmptyTitle: 'ยังไม่มีโปรไฟล์', bkEmptyText: 'ตั้งค่า wizard ให้เป็นสไตล์ช่องของคุณ แล้วบันทึกที่นี่',
            bkSaved: 'บันทึกโปรไฟล์แล้ว', bkApplied: 'ใช้ "{name}" กับ wizard แล้ว', bkDeleted: 'ลบโปรไฟล์แล้ว', bkNeedName: 'ตั้งชื่อโปรไฟล์ก่อน',
            bkConfirmDelete: 'ลบโปรไฟล์ "{name}" หรือไม่?', bkFailed: 'ข้อผิดพลาดของโปรไฟล์',
            engineRecap: 'รีแคป', engineSubtitle: 'คำบรรยาย', engineHardsub: 'ฮาร์ดซับ',
            statusRunning: 'กำลังทำงาน', statusDone: 'เสร็จ', statusError: 'ผิดพลาด', statusCancelled: 'ยกเลิก', statusQueued: 'อยู่ในคิว',
            COMPLETED: 'เสร็จสมบูรณ์', COMPLETED_WITH_WARNINGS: 'เสร็จพร้อมคำเตือน', FAILED: 'ล้มเหลว', CANCELLED: 'ยกเลิกแล้ว',
            RUNNING: 'กำลังทำงาน', QA_PENDING: 'รอ QA', QUEUED: 'อยู่ในคิว',
            issue_missing: 'ไม่มีคำแปล', issue_placeholder: 'ข้อความว่าง', issue_meta_text: 'ข้อความอธิบายเกิน',
            issue_untranslated: 'ยังไม่แปล', issue_foreign_script: 'มีอักษรต่างชาติ', issue_latin_words: 'เหลือคำอังกฤษ',
            issue_copied_source: 'คัดลอกจากต้นฉบับ', issue_duplicate: 'ซ้ำบรรทัดก่อนหน้า', issue_repeat_boundary: 'พูดซ้ำบรรทัดก่อนหน้า',
            keyTest: 'ทดสอบคีย์', keyChecking: 'กำลังตรวจสอบ...', keyOnline: 'ใช้งานได้', keyInvalid: 'คีย์ไม่ถูกต้อง', keyRate: 'ถูกต้อง แต่ติดขีดจำกัด',
            keyUnreachable: 'เชื่อมต่อ Google ไม่ได้', keyNoKeys: 'วางคีย์อย่างน้อยหนึ่งรายการก่อน',
            keyAllGood: 'คีย์ทั้ง {n} รายการใช้งานได้ กด Save Keys เพื่อบันทึก', keySomeBad: '{bad} จาก {n} คีย์ใช้งานไม่ได้', keyFailed: 'ทดสอบคีย์ไม่ได้',
            dictTitle: 'พจนานุกรมการออกเสียง', dictDesc: 'แก้ชื่อและคำยืมที่เสียงอ่านผิด เปลี่ยนเฉพาะข้อความที่พูด สคริปต์และคำบรรยายยังคงการสะกดเดิม',
            dictLabel: 'หนึ่งรายการต่อบรรทัด: ต้นฉบับ => วิธีอ่านที่ต้องการ', dictHint: 'คำภาษาอังกฤษจับคู่ทั้งคำโดยไม่สนตัวพิมพ์ใหญ่เล็ก คำภาษาพม่าจับคู่ได้ทุกตำแหน่งในบรรทัด',
            dictSave: 'บันทึกพจนานุกรม', dictSaved: 'บันทึก {n} รายการแล้ว บรรทัดที่มีคำเหล่านี้จะพากย์ใหม่ในการรันครั้งถัดไป', dictFailed: 'บันทึกพจนานุกรมไม่ได้',
            stTitle: 'ข้อมูลเรื่องและการเปลี่ยนชื่อ', stEmpty: 'โปรเจกต์นี้ไม่มีข้อมูลเรื่อง (คลิปสั้น หรือสร้างก่อนมี story memory)',
            stSummary: 'สรุป', stTone: 'น้ำเสียง', stCharacters: 'ตัวละคร', stPlaces: 'สถานที่', stTerms: 'คำศัพท์', stRelations: 'ความสัมพันธ์',
            stClickHint: 'คลิกชื่อเพื่อเปลี่ยนทั่วทั้งสคริปต์',
            rpFind: 'ค้นหา', rpWith: 'แทนที่ด้วย', rpMatchCase: 'ตรงตัวพิมพ์', rpWholeWord: 'ทั้งคำ (อังกฤษ)', rpPreview: 'ดูตัวอย่าง', rpApply: 'แทนที่ทั้งหมด',
            rpNone: 'ไม่มีบรรทัดที่มีคำนี้', rpFound: 'พบ {lines} บรรทัด {n} แห่ง', rpDone: 'แทนที่ใน {lines} บรรทัดแล้ว กดบันทึกและเรนเดอร์ใหม่เพื่อฟัง',
            rpConfirm: 'แทนที่ใน {lines} บรรทัดหรือไม่?', rpSaveFirst: 'บันทึกการแก้ไขก่อน แล้วค่อยแทนที่', rpNeedFind: 'ใส่ข้อความที่ต้องการค้นหา', rpFailed: 'แทนที่ไม่สำเร็จ',
            optStoryLength: 'ความยาวเรื่อง (สไตล์รีแคป)', optLenStandard: 'มาตรฐาน', optLenCompact: 'กระชับ - เฉพาะเหตุการณ์สำคัญ', optLenDetailed: 'ละเอียด - การกระทำและเหตุการณ์ย่อย', optLenScene: 'ทีละฉาก - ยาวที่สุด',
            optNarrativeTone: 'น้ำเสียงการเล่า (สไตล์รีแคป)', optToneNatural: 'ธรรมชาติ', optToneBrisk: 'กระฉับกระเฉง', optToneCalm: 'สงบ - ครุ่นคิด', optToneDramatic: 'ดราม่า - ลุ้นระทึก',
            optTtsRate: 'ความเร็วการเล่า', optRateDefault: 'ค่าเริ่มต้น (จาก config)', optRateSlow: 'ช้า (-15%)', optRateNormal: 'ปกติ (0%)', optRateFast: 'เร็ว (+15%)', optRateVeryFast: 'เร็วมาก (+30%)',
            pjCapcut: 'CapCut', capBuilding: 'กำลังสร้างแพ็กเกจ CapCut...', capReady: 'แพ็กเกจ CapCut พร้อมแล้ว ({mb} MB) กำลังดาวน์โหลด',
            capFailed: 'สร้างแพ็กเกจ CapCut ไม่ได้',
            optDelivery: 'ต้องการรับงานแบบไหน?', optDeliveryOneclick: 'วิดีโอสำเร็จรูปในคลิกเดียว',
            optDeliveryOneclickDesc: 'ทำให้ตั้งแต่ต้นจนจบ ได้วิดีโอที่เรนเดอร์เสร็จแล้ว',
            optDeliveryCapcut: 'CapCut pipeline',
            optDeliveryCapcutDesc: 'ข้ามการเรนเดอร์ขั้นสุดท้ายที่ช้า ได้แพ็กเกจไปตัดต่อใน CapCut: แทร็กเสียงบรรยาย คำบรรยายภาษาพม่าที่ไม่เพี้ยน และคู่มือ',
            optDeliveryBoth: 'ทั้งสองแบบ', optDeliveryBothDesc: 'ได้ทั้งวิดีโอสำเร็จรูปและแพ็กเกจ CapCut',
            optDeliveryNoteCapcut: 'โหมดนี้ไม่เรนเดอร์วิดีโอสุดท้าย จึงไม่ใช้ตัวเลือก Deliverable ด้านล่าง งานจบด้วย capcut_package.zip',
            optDeliveryNoteBoth: 'ใช้เวลาเท่ากับวิดีโอสำเร็จรูป แล้วเพิ่ม capcut_package.zip',
            revDelivery: 'การส่งมอบ: {mode}', dlCapcut: 'ดาวน์โหลดแพ็กเกจ CapCut', capDoneToast: 'แพ็กเกจ CapCut พร้อมแล้ว กำลังดาวน์โหลด',
            engineShorts: 'Shorts', engineSplitter: 'Splitter', pjClips: '{n} Shorts', pjParts: '{n} ส่วน', pjResults: 'ผลลัพธ์',
            issue_over_budget: 'ยาวเกิน'
        }
    };

    function t(key, vars) {
        const dict = I18N[lang()] || I18N.en;
        let text = dict[key] !== undefined ? dict[key] : (I18N.en[key] !== undefined ? I18N.en[key] : key);
        for (const [name, value] of Object.entries(vars || {})) text = text.split('{' + name + '}').join(String(value));
        return text;
    }

    /** Fills every element tagged data-st="key" / data-st-placeholder="key" for the current language. */
    function applyLanguage() {
        document.querySelectorAll('[data-st]').forEach(node => { node.textContent = t(node.dataset.st); });
        document.querySelectorAll('[data-st-placeholder]').forEach(node => { node.placeholder = t(node.dataset.stPlaceholder); });
        const titleText = $('header-title-text');
        if (titleText && state.view) titleText.textContent = viewTitle(state.view) || titleText.textContent;
        // dynamic parts re-render in the new language
        if (state.view === 'projects') renderProjects();
        if (state.view === 'brand') renderProfiles();
        if (state.view === 'editor') updateEditorChrome();
        document.dispatchEvent(new CustomEvent('pai:language', { detail: lang() }));   // static/js/tools.js re-renders its texts
    }

    function viewTitle(view) {
        return { projects: t('titleProjects'), editor: t('titleEditor'), brand: t('titleBrand') }[view] || null;
    }


    // ── state ─────────────────────────────────────────────────────────────────
    const state = {
        view: null,
        projects: [],
        projectsLoaded: false,
        projectsTab: 'projects',
        profiles: [],
        editor: { project: null, blocks: [], dirty: new Map(), filter: 'all', query: '' },
        storage: null
    };


    // ── theme ─────────────────────────────────────────────────────────────────
    function toggleTheme() {
        const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        try { localStorage.setItem('pai_theme', next); } catch (e) { /* storage blocked: the choice lasts for this visit */ }
    }


    // ── storage meter ─────────────────────────────────────────────────────────
    async function loadStorage() {
        try {
            const data = await api('/api/storage');
            state.storage = data;
            const total = Math.max(data.total_mb, 0.001);
            const pct = key => (100 * (data[key] || 0) / total).toFixed(1) + '%';
            $('storage-total').textContent = formatMb(data.total_mb);
            $('storage-seg-outputs').style.width = pct('outputs');
            $('storage-seg-movies').style.width = pct('movies');
            $('storage-seg-temp').style.width = pct('temp');
            $('storage-legend').textContent = 'outputs ' + formatMb(data.outputs) + ' · movies ' + formatMb(data.movies) + ' · temp ' + formatMb(data.temp);
            $('storage-meter').classList.remove('hidden');
            if (state.view === 'projects') renderProjectStats();
        } catch (e) { /* the meter is a convenience: stay hidden when the endpoint is unavailable */ }
    }

    function formatMb(mb) {
        const value = Number(mb) || 0;
        return value >= 1024 ? (value / 1024).toFixed(1) + ' GB' : Math.round(value) + ' MB';
    }


    // ── projects ──────────────────────────────────────────────────────────────
    async function loadProjects() {
        try {
            const data = await api('/api/projects');
            state.projects = data.projects || [];
            state.projectsLoaded = true;
            updateNavCount();
            if (state.view === 'projects') renderProjects();
        } catch (e) {
            if (state.view === 'projects') {
                $('pj-grid').replaceChildren(el('div', { class: 'empty-state', text: e.message }));
            }
        }
    }

    function updateNavCount() {
        const total = state.projects.reduce((sum, p) => sum + (p.needs_review || 0), 0);
        const badge = $('nav-projects-count');
        if (!badge) return;
        badge.textContent = total > 99 ? '99+' : String(total);
        badge.classList.toggle('hidden', total === 0);
    }

    function verdictClass(verdict) {
        return ['ready', 'review', 'attention'].includes(verdict) ? verdict : '';
    }

    function renderProjectStats() {
        const projects = state.projects;
        const review = projects.reduce((sum, p) => sum + (p.needs_review || 0), 0);
        const ready = projects.filter(p => p.verdict === 'ready').length;
        const stat = (label, value, small) => el('div', { class: 'stat' },
            el('div', { class: 'stat-label', text: label }),
            el('div', { class: 'stat-value' }, String(value), small ? el('small', { text: ' ' + small }) : null));
        $('pj-stats').replaceChildren(
            stat(t('statProjects'), projects.length),
            stat(t('statReview'), review),
            stat(t('statReady'), ready),
            stat(t('statStorage'), state.storage ? formatMb(state.storage.total_mb) : '-')
        );
    }

    function renderProjects() {
        renderProjectStats();
        const grid = $('pj-grid');
        if (!state.projectsLoaded) return;
        if (!state.projects.length) {
            grid.replaceChildren(el('div', { class: 'empty-state' },
                el('strong', { text: t('pjEmptyTitle') }), t('pjEmptyText')));
            return;
        }
        grid.replaceChildren(...state.projects.map(projectCard));
    }

    const CAPCUT_ENGINES = ['recap', 'subtitle', 'hardsub', 'shorts'];   // services/capcut_export.supported_engines()
    const TOOL_ENGINES = ['shorts', 'splitter'];

    function projectCard(project) {
        const score = project.score;
        const ring = el('div', { class: 'score-ring ' + verdictClass(project.verdict), style: '--pct:' + (score === null || score === undefined ? 0 : score) },
            el('span', { text: score === null || score === undefined ? '-' : String(score) }));
        const engineLabel = { recap: t('engineRecap'), subtitle: t('engineSubtitle'), hardsub: t('engineHardsub'),
            shorts: t('engineShorts'), splitter: t('engineSplitter') }[project.engine_type] || project.engine_type;
        const isTool = TOOL_ENGINES.includes(project.engine_type);
        const countLabel = project.engine_type === 'shorts' ? 'pjClips' : (project.engine_type === 'splitter' ? 'pjParts' : 'pjLines');
        const meta = el('div', { class: 'pj-meta' },
            el('span', { class: 'chip chip-gold', text: engineLabel }),
            project.status ? el('span', { class: 'chip', text: statusLabel(project.status) }) : null,
            project.lines ? el('span', { text: t(countLabel, { n: project.lines }) }) : null,
            project.duration ? el('span', { text: project.duration }) : null);
        const flag = project.needs_review
            ? el('span', { class: 'chip chip-warn', text: t('pjToReview', { n: project.needs_review }) })
            : (project.lines && !isTool ? el('span', { class: 'chip chip-ok', text: t('pjClean') }) : null);

        const actions = el('div', { class: 'pj-actions' });
        if (project.engine_type === 'recap' && project.lines) {
            actions.append(el('button', { type: 'button', class: 'btn-primary btn-sm', text: t('pjEdit'), onclick: () => openEditor(project.name) }));
        }
        if (isTool) {      // Splitter / Long to Short: the parts or Shorts it made, not a quality report
            actions.append(el('button', { type: 'button', class: 'btn-primary btn-sm', text: t('pjResults'), onclick: () => window.PaiTools && window.PaiTools.openResult(project.name) }));
        } else {
            actions.append(el('button', { type: 'button', class: 'btn-secondary btn-sm', text: t('pjQuality'), onclick: () => openQuality(project.name) }));
        }
        if (project.engine_type === 'recap' && project.lines) {
            actions.append(el('button', { type: 'button', class: 'btn-secondary btn-sm', text: t('pjRerender'), onclick: () => rerender(project.name) }));
        }
        if (CAPCUT_ENGINES.includes(project.engine_type) && project.lines) {      // every studio can be packaged for CapCut
            actions.append(el('button', { type: 'button', class: 'btn-secondary btn-sm', text: t('pjCapcut'), onclick: event => exportCapcut(project.name, event.currentTarget) }));
        }
        actions.append(el('button', { type: 'button', class: 'btn-nav btn-sm', text: t('pjFiles'), onclick: () => window.switchView('outputs') }));

        return el('article', { class: 'pj-card' },
            el('div', { class: 'pj-card-head' }, ring,
                el('div', { style: 'min-width:0;flex:1;' }, el('div', { class: 'pj-title', text: project.movie_name || project.name }), meta)),
            el('div', { class: 'row-between' }, flag || el('span'),
                el('span', { class: 'muted', style: 'font-size:0.74rem;', text: t('pjUpdated', { when: formatDate(project.updated) }) })),
            actions);
    }

    function showProjectsTab(tab) {
        state.projectsTab = tab;
        $('pj-tab-projects').classList.toggle('active', tab === 'projects');
        $('pj-tab-history').classList.toggle('active', tab === 'history');
        $('pj-panel-projects').classList.toggle('hidden', tab !== 'projects');
        $('pj-panel-history').classList.toggle('hidden', tab !== 'history');
        $('pj-stats').classList.toggle('hidden', tab !== 'projects');
        if (tab === 'history') loadHistory();
    }

    async function loadHistory() {
        const body = $('hist-body');
        try {
            const data = await api('/api/jobs/history?limit=100');
            const jobs = data.jobs || [];
            if (!jobs.length) {
                body.replaceChildren(el('tr', {}, el('td', { colspan: 4 }, el('div', { class: 'empty-state', style: 'border:0;', text: t('histEmpty') }))));
                return;
            }
            body.replaceChildren(...jobs.map(job => {
                const status = String(job.status || '');
                const cls = { done: 'chip-ok', running: 'chip-info', error: 'chip-err', cancelled: 'chip-warn', queued: 'chip' }[status] || 'chip';
                const name = String(job.input_source || job.job_id).split(/[\\/]/).pop();
                return el('tr', {},
                    el('td', {}, el('div', { style: 'font-weight:650;word-break:break-word;', text: name }),
                        el('div', { class: 'mono', text: String(job.job_id).slice(0, 8) })),
                    el('td', {}, el('span', { class: 'chip ' + cls, text: I18N.en['status' + status.charAt(0).toUpperCase() + status.slice(1)] ? t('status' + status.charAt(0).toUpperCase() + status.slice(1)) : status })),
                    el('td', { text: job.phase || '-' }),
                    el('td', { class: 'mono', text: formatDate(job.created_at) }));
            }));
        } catch (e) {
            body.replaceChildren(el('tr', {}, el('td', { colspan: 4, text: e.message })));
        }
    }

    function refreshProjects() {
        loadStorage();
        loadProjects();
        if (state.projectsTab === 'history') loadHistory();
    }

    async function rerender(name) {
        try {
            const data = await api('/api/projects/' + encodeURIComponent(name) + '/rerender', { method: 'POST' });
            toast(t('seRerenderStarted'), 'success');
            watchJob(data.job_id, name);
        } catch (e) {
            toast(e.message, 'error');
        }
    }

    /** Shows the live monitor (wizard step 6) for a job started from outside the wizard. */
    function watchJob(jobId, projectName) {
        if (!jobId) return;
        if (typeof currentJobConfig !== 'undefined') {
            currentJobConfig = {
                engine: 'recap', translationStyle: '-', audioMode: '-', sfxMode: '-', sfxVolume: 0, videoFormat: '-',
                activeSubtitlePreset: '-', resolution: '-', customTags: ['Project: ' + projectName, 'Re-render with saved settings', 'Resume: only changes']
            };
            if (typeof renderActiveJobConfigBox === 'function') renderActiveJobConfigBox();
        }
        window.switchView('studio');
        window.goToStep(6);
        if (typeof window.setProgressBar === 'function') window.setProgressBar(5);
        if (typeof window.setLog === 'function') window.setLog('Re-rendering ' + projectName + '...');
        window.startStreamingLogs(jobId);
    }


    async function exportCapcut(name, button) {
        if (button) button.disabled = true;
        toast(t('capBuilding'));
        try {
            const data = await api('/api/projects/' + encodeURIComponent(name) + '/capcut', { method: 'POST' });
            toast(t('capReady', { mb: data.size_mb }), 'success');
            window.location.href = data.download;
        } catch (e) {
            toast(t('capFailed') + ': ' + e.message, 'error');
        } finally {
            if (button) button.disabled = false;
        }
    }


    // ── quality modal ─────────────────────────────────────────────────────────
    async function openQuality(name) {
        const body = $('quality-modal-body');
        body.replaceChildren(el('div', { class: 'empty-state' }, el('span', { class: 'spinner' })));
        $('quality-modal-title').textContent = t('qTitle') + ' - ' + name;
        $('quality-modal-backdrop').classList.add('open');
        try {
            const report = await api('/api/projects/' + encodeURIComponent(name) + '/quality');
            body.replaceChildren(...qualityBody(report));
            loadProjects();    // the report was saved: refresh the scores
        } catch (e) {
            body.replaceChildren(el('div', { class: 'empty-state', text: t('qFailed') + ': ' + e.message }));
        }
    }

    function closeQuality() {
        $('quality-modal-backdrop').classList.remove('open');
    }

    function kv(pairs) {
        const dl = el('dl', { class: 'kv' });
        for (const [label, value] of pairs) {
            if (value === null || value === undefined || value === '') continue;
            dl.append(el('dt', { text: label }), el('dd', { text: String(value) }));
        }
        return dl;
    }

    /** Pipeline status ("COMPLETED_WITH_WARNINGS", "Completed with warnings"...) in the current language when known. */
    function statusLabel(status) {
        const key = String(status || '').toUpperCase().replace(/ /g, '_');
        return I18N.en[key] ? t(key) : status;
    }

    function qualityBody(report) {
        const verdict = verdictClass(report.verdict);
        const score = report.score;
        const verdictText = { ready: t('qVerdictReady'), review: t('qVerdictReview'), attention: t('qVerdictAttention') }[verdict] || t('verdictUnknown');
        const parts = [
            el('div', { class: 'row', style: 'gap:1.1rem;margin-bottom:1.2rem;' },
                el('div', { class: 'score-ring big ' + verdict, style: '--pct:' + (score || 0) }, el('span', { text: score === null || score === undefined ? '-' : String(score) })),
                el('div', {}, el('div', { style: 'font-weight:800;font-size:1.1rem;color:var(--primary);', text: verdictText }),
                    el('div', { class: 'muted', style: 'font-size:0.82rem;', text: formatDate(report.generated_at) })))
        ];

        const pipeline = report.pipeline || {};
        parts.push(el('div', { class: 'card', style: 'padding:1rem 1.2rem;margin-bottom:1rem;' }, kv([
            [t('qStatus'), statusLabel(pipeline.status)], [t('qDuration'), pipeline.duration],
            [t('qVideo'), report.video && report.video.final_recap_mb ? report.video.final_recap_mb + ' MB' : null]
        ])));

        if (report.script) {
            const s = report.script, v = report.voice || {};
            parts.push(el('div', { class: 'card', style: 'padding:1rem 1.2rem;margin-bottom:1rem;' },
                el('div', { class: 'card-title', style: 'margin-bottom:0.7rem;font-size:0.95rem;', text: t('qScript') }),
                kv([[t('qLines'), s.lines], [t('qNeedsReview'), s.needs_review], [t('qOverBudget'), s.over_budget], [t('qEdited'), s.edited],
                    [t('qClips'), v.clips], [t('qMissing'), v.missing_clips], [t('qOverflow'), v.overflowing_clips]]),
                Object.keys(s.issues || {}).length ? el('div', { class: 'row', style: 'margin-top:0.8rem;' },
                    Object.entries(s.issues).map(([code, n]) => el('span', { class: 'chip chip-warn', text: t('issue_' + code) + ' · ' + n }))) : null));
            if ((s.review_lines || []).length) {
                parts.push(el('div', { class: 'card-title', style: 'font-size:0.92rem;margin:0.4rem 0 0.6rem;', text: t('qReviewLines') }));
                parts.push(reviewTable(s.review_lines, report.project, 'narration'));
            }
        } else if (report.subtitles) {
            const s = report.subtitles;
            parts.push(el('div', { class: 'card', style: 'padding:1rem 1.2rem;margin-bottom:1rem;' },
                el('div', { class: 'card-title', style: 'margin-bottom:0.7rem;font-size:0.95rem;', text: t('qSubtitles') }),
                kv([[t('qLines'), s.lines], [t('qNeedsReview'), s.needs_review]])));
            if ((s.review_lines || []).length) {
                parts.push(el('div', { class: 'card-title', style: 'font-size:0.92rem;margin:0.4rem 0 0.6rem;', text: t('qReviewLines') }));
                parts.push(reviewTable(s.review_lines, report.project, 'burmese'));
            }
        }

        for (const [title, list] of [[t('qWarnings'), pipeline.warnings], [t('qErrors'), pipeline.errors]]) {
            if (list && list.length) {
                parts.push(el('div', { class: 'card-title', style: 'font-size:0.92rem;margin:0.8rem 0 0.4rem;', text: title }));
                parts.push(el('ul', { style: 'padding-left:1.2rem;font-size:0.82rem;color:var(--text-secondary);' }, list.map(item => el('li', { text: String(item) }))));
            }
        }
        return parts;
    }

    function reviewTable(rows, projectName, textKey) {
        const tbody = el('tbody');
        rows.forEach(row => {
            tbody.append(el('tr', {},
                el('td', { class: 'mono', text: '#' + (row.index + 1) }),
                el('td', { style: 'max-width:240px;word-break:break-word;', text: row.source_text || row.original || '' }),
                el('td', { style: 'max-width:240px;word-break:break-word;', text: row[textKey] || '' }),
                el('td', {}, (row.issues || []).map(code => el('span', { class: 'chip chip-warn', style: 'margin:0 0.2rem 0.2rem 0;', text: t('issue_' + code) })))));
        });
        return el('div', { class: 'table-wrap' }, el('table', { class: 'data-table' },
            el('thead', {}, el('tr', {}, el('th', { text: '#' }), el('th', { text: t('seSource') }), el('th', { text: 'Narration' }), el('th', { text: '' }))),
            tbody));
    }


    // ── script editor ─────────────────────────────────────────────────────────
    async function openEditor(name) {
        const editor = state.editor;
        editor.project = name;
        editor.blocks = [];
        editor.dirty = new Map();
        editor.filter = 'all';
        editor.query = '';
        $('se-search').value = '';
        $('se-title').textContent = t('titleEditor') + ' - ' + name;
        $('se-list').replaceChildren(el('div', { class: 'empty-state' }, el('span', { class: 'spinner' })));
        window.switchView('editor');
        updateFilterButtons();
        try {
            const data = await api('/api/projects/' + encodeURIComponent(name) + '/script');
            if (!data.editable) {
                $('se-list').replaceChildren(el('div', { class: 'empty-state', text: data.reason || t('seNotEditable') }));
                return;
            }
            editor.blocks = data.blocks;
            renderEditor();
            renderBible(data.story);
        } catch (e) {
            $('se-list').replaceChildren(el('div', { class: 'empty-state', text: t('seLoadFailed') + ': ' + e.message }));
        }
    }

    function renderEditor() {
        const list = $('se-list');
        if (!state.editor.blocks.length) {
            list.replaceChildren(el('div', { class: 'empty-state', text: t('seEmpty') }));
            return;
        }
        list.replaceChildren(...state.editor.blocks.map(editorRow));
        applyEditorFilter();
        updateEditorChrome();
    }

    function editorRow(block) {
        const idx = block.index;
        const textarea = el('textarea', {
            class: 'input-text textarea-control se-ta', rows: Math.max(2, Math.ceil(block.narration.length / 46)),
            'data-index': idx, 'aria-label': 'Narration ' + (idx + 1), spellcheck: 'false'
        });
        textarea.value = block.narration;
        textarea.addEventListener('input', () => { autosize(textarea); markDirty(idx, { narration: textarea.value }); });
        textarea.addEventListener('focus', () => autosize(textarea));

        const gender = el('select', { class: 'select-control', 'aria-label': 'Voice ' + (idx + 1) },
            el('option', { value: 'male', text: t('seMale') }), el('option', { value: 'female', text: t('seFemale') }));
        gender.value = block.gender === 'female' ? 'female' : 'male';
        gender.addEventListener('change', () => markDirty(idx, { gender: gender.value }));

        const count = el('span', { class: 'se-count', 'data-count': idx });
        const side = el('div', { class: 'se-side' }, gender, count,
            el('div', { class: 'se-issues' }, (block.issues || []).map(code => el('span', { class: 'chip chip-warn', text: t('issue_' + code) }))));
        if (block.needs_review) {
            side.append(el('button', {
                type: 'button', class: 'btn-nav btn-sm', text: t('seMarkChecked'),
                onclick: () => markDirty(idx, { narration: textarea.value, checked: true })
            }));
        }

        const row = el('div', { class: 'se-row', 'data-index': idx },
            el('div', { class: 'se-no' }, '#' + (idx + 1)),
            el('div', { class: 'se-src' }, el('span', { class: 'se-time', text: clock(block.start_sec) + ' → ' + clock(block.end_sec) }), block.source_text || ''),
            el('div', { class: 'se-edit' }, textarea),
            side);
        paintRow(row, block, block.narration);
        return row;
    }

    function autosize(textarea) {
        textarea.style.height = 'auto';
        textarea.style.height = (textarea.scrollHeight + 2) + 'px';
    }

    function paintRow(row, block, narration) {
        const over = narration.length > block.max_chars * 1.45;
        const tight = !over && narration.length > block.max_chars * 1.1;
        row.classList.toggle('is-over', over);
        row.classList.toggle('is-flagged', !over && block.needs_review && !state.editor.dirty.has(block.index));
        row.classList.toggle('is-dirty', state.editor.dirty.has(block.index));
        const count = row.querySelector('.se-count');
        count.textContent = narration.length + ' / ' + block.max_chars;
        count.classList.toggle('over', over);
        count.classList.toggle('tight', tight);
        count.title = over ? t('seTooLong') : '';
    }

    function markDirty(index, change) {
        const editor = state.editor;
        const block = editor.blocks[index];
        const current = Object.assign({}, editor.dirty.get(index) || {}, change);
        const row = document.querySelector('.se-row[data-index="' + index + '"]');
        const narrationChanged = current.narration !== undefined && current.narration.trim() !== block.narration;
        const genderChanged = current.gender !== undefined && current.gender !== (block.gender === 'female' ? 'female' : 'male');
        if (narrationChanged || genderChanged || current.checked) editor.dirty.set(index, current);
        else editor.dirty.delete(index);
        if (row) paintRow(row, block, current.narration !== undefined ? current.narration : block.narration);
        updateEditorChrome();
    }

    function updateEditorChrome() {
        const editor = state.editor;
        const n = editor.dirty.size;
        const chip = $('se-dirty');
        chip.textContent = n ? t('seDirty', { n }) : t('seNoChanges');
        chip.className = 'chip ' + (n ? 'chip-gold' : '');
        $('se-save').disabled = n === 0;
        $('se-save-render').disabled = false;    // "re-render" is also useful with nothing new to save
        $('se-count-review').textContent = String(editor.blocks.filter(b => b.needs_review).length);
        $('se-count-over').textContent = String(editor.blocks.filter(b => b.over_budget).length);
    }

    function updateFilterButtons() {
        document.querySelectorAll('[data-se-filter]').forEach(btn => btn.classList.toggle('active', btn.dataset.seFilter === state.editor.filter));
    }

    function applyEditorFilter() {
        const { blocks, filter, query } = state.editor;
        const needle = query.trim().toLowerCase();
        let shown = 0;
        document.querySelectorAll('#se-list .se-row').forEach(row => {
            const block = blocks[Number(row.dataset.index)];
            const narration = row.querySelector('textarea').value;
            let visible = true;
            if (filter === 'review') visible = block.needs_review;
            else if (filter === 'over') visible = narration.length > block.max_chars * 1.45;
            else if (filter === 'edited') visible = block.edited || state.editor.dirty.has(block.index);
            if (visible && needle) visible = (narration + ' ' + (block.source_text || '')).toLowerCase().includes(needle);
            row.classList.toggle('hidden', !visible);
            if (visible) shown += 1;
        });
        const list = $('se-list');
        const empty = list.querySelector('.se-empty');
        if (!shown && !empty) list.append(el('div', { class: 'empty-state se-empty', text: t('seEmpty') }));
        if (shown && empty) empty.remove();
    }

    async function saveScript(thenRerender) {
        const editor = state.editor;
        if (!editor.project) return;
        const edits = [];
        editor.dirty.forEach((change, index) => {
            const edit = { index };
            if (change.narration !== undefined) edit.narration = change.narration;
            if (change.gender !== undefined) edit.gender = change.gender;
            edits.push(edit);
        });
        if (!edits.length && !thenRerender) { toast(t('seNothing')); return; }
        try {
            if (edits.length) {
                const result = await api('/api/projects/' + encodeURIComponent(editor.project) + '/script', {
                    method: 'PUT', body: JSON.stringify({ edits })
                });
                toast(t('seSaved', { n: result.changed }), 'success');
                editor.dirty = new Map();
                const data = await api('/api/projects/' + encodeURIComponent(editor.project) + '/script');
                editor.blocks = data.blocks;
                renderEditor();
                renderBible(data.story);
                loadProjects();
            }
            if (thenRerender) await rerender(editor.project);
        } catch (e) {
            toast(t('seSaveFailed') + ': ' + e.message, 'error');
        }
    }

    // ── story bible + find & replace ──────────────────────────────────────────
    function renderBible(story) {
        const box = $('se-bible');
        if (!story) { box.replaceChildren(el('div', { class: 'form-hint', text: t('stEmpty') })); return; }
        const block = (label, ...children) => el('div', { class: 'bible-block' }, el('div', { class: 'stat-label', text: label }), ...children);
        const parts = [];
        if (story.summary) parts.push(block(t('stSummary'), el('div', { style: 'font-size:0.88rem;', text: story.summary })));
        if (story.tone) parts.push(block(t('stTone'), el('div', { style: 'font-size:0.88rem;', text: story.tone })));
        const nameChip = item => el('button', {
            type: 'button', class: 'bible-chip', title: t('stClickHint'),
            onclick: () => { $('rp-find').value = item.target; $('rp-with').value = item.target; $('rp-with').focus(); $('rp-with').select(); }
        }, item.target || item.name, el('small', { text: item.name + (item.details ? ' \u00b7 ' + item.details : '') }));
        for (const [key, label] of [['characters', 'stCharacters'], ['places', 'stPlaces'], ['terms', 'stTerms']]) {
            if ((story[key] || []).length) parts.push(block(t(label), el('div', { class: 'bible-chips' }, story[key].map(nameChip))));
        }
        if ((story.relationships || []).length) parts.push(block(t('stRelations'), el('div', { style: 'font-size:0.84rem;color:var(--text-secondary);', text: story.relationships.join(' \u00b7 ') })));
        parts.push(el('div', { class: 'form-hint', text: t('stClickHint') }));
        box.replaceChildren(...parts);
    }

    function replaceBody(preview) {
        return JSON.stringify({ find: $('rp-find').value, replace: $('rp-with').value, match_case: $('rp-case').checked, whole_word: $('rp-word').checked, preview });
    }

    async function previewReplace() {
        const out = $('rp-result');
        if (!$('rp-find').value.trim()) { out.textContent = t('rpNeedFind'); return null; }
        try {
            const res = await api('/api/projects/' + encodeURIComponent(state.editor.project) + '/script/replace', { method: 'POST', body: replaceBody(true) });
            out.replaceChildren(res.lines ? t('rpFound', { lines: res.lines, n: res.occurrences }) : t('rpNone'),
                ...(res.samples || []).slice(0, 3).map(s => el('div', { class: 'mono', style: 'margin-top:0.35rem;word-break:break-word;' },
                    '#' + (s.index + 1) + '  ', el('s', { text: s.before.slice(0, 80) }), '  \u2192  ', s.after.slice(0, 80))));
            return res;
        } catch (e) {
            out.textContent = t('rpFailed') + ': ' + e.message;
            return null;
        }
    }

    async function applyReplace() {
        const editor = state.editor;
        if (editor.dirty.size) { toast(t('rpSaveFirst'), 'warn'); return; }
        const preview = await previewReplace();
        if (!preview || !preview.lines) return;
        if (!window.confirm(t('rpConfirm', { lines: preview.lines }))) return;
        try {
            await api('/api/projects/' + encodeURIComponent(editor.project) + '/script/replace', { method: 'POST', body: replaceBody(false) });
            toast(t('rpDone', { lines: preview.lines }), 'success');
            $('rp-result').textContent = '';
            const data = await api('/api/projects/' + encodeURIComponent(editor.project) + '/script');
            editor.blocks = data.blocks;
            renderEditor();
            renderBible(data.story);
            loadProjects();
        } catch (e) {
            toast(t('rpFailed') + ': ' + e.message, 'error');
        }
    }

    function bindEditor() {
        $('se-search').addEventListener('input', event => { state.editor.query = event.target.value; applyEditorFilter(); });
        document.querySelectorAll('[data-se-filter]').forEach(btn => btn.addEventListener('click', () => {
            state.editor.filter = btn.dataset.seFilter;
            updateFilterButtons();
            applyEditorFilter();
        }));
        window.addEventListener('beforeunload', event => {
            if (state.view === 'editor' && state.editor.dirty.size) { event.preventDefault(); event.returnValue = ''; }
        });
    }


    // ── brand kit (profiles) ──────────────────────────────────────────────────
    async function loadProfiles() {
        try {
            state.profiles = (await api('/api/profiles')).profiles || [];
            renderProfiles();
        } catch (e) {
            $('bk-grid').replaceChildren(el('div', { class: 'empty-state', text: e.message }));
        }
    }

    function describeOption(name, value) {
        if (value === null || value === undefined || value === '') return null;
        if (typeof value === 'boolean') return value ? name.replace(/_/g, ' ') : null;
        if (typeof value === 'object') return null;
        return name.replace(/_/g, ' ') + ': ' + value;
    }

    function renderProfiles() {
        const grid = $('bk-grid');
        if (!state.profiles.length) {
            grid.replaceChildren(el('div', { class: 'empty-state' }, el('strong', { text: t('bkEmptyTitle') }), t('bkEmptyText')));
            return;
        }
        grid.replaceChildren(...state.profiles.map(profile => {
            const tags = Object.entries(profile.options || {}).map(([k, v]) => describeOption(k, v)).filter(Boolean).slice(0, 9);
            const engineLabel = { recap: t('engineRecap'), subtitle: t('engineSubtitle'), hardsub: t('engineHardsub') }[profile.engine_mode] || profile.engine_mode;
            return el('article', { class: 'bk-card' },
                el('div', { class: 'row-between' }, el('div', { class: 'bk-name', text: profile.name }), el('span', { class: 'chip chip-gold', text: engineLabel })),
                el('div', { class: 'bk-tags' }, tags.map(tag => el('span', { class: 'chip', text: tag }))),
                el('div', { class: 'pj-actions' },
                    el('button', { type: 'button', class: 'btn-primary btn-sm', text: t('bkApply'), onclick: () => applyProfile(profile) }),
                    el('button', { type: 'button', class: 'btn-danger btn-sm', text: t('bkDelete'), onclick: () => deleteProfile(profile) })));
        }));
    }

    function profileOptionsFromWizard() {
        if (typeof collectJobOptions !== 'function') return {};
        const all = collectJobOptions();
        const engine = all.engine_mode || 'recap';
        const options = {};
        for (const [name, value] of Object.entries(all)) {
            if (PER_VIDEO_OPTIONS.includes(name)) continue;
            const spec = typeof OPTION_MODEL !== 'undefined' && OPTION_MODEL && OPTION_MODEL.options[name];
            if (spec && !spec.engines.includes(engine)) continue;      // the server rejects options the engine does not use
            options[name] = value;
        }
        return options;
    }

    async function saveProfileFromWizard() {
        const name = $('bk-name').value.trim();
        if (!name) { toast(t('bkNeedName'), 'warn'); $('bk-name').focus(); return; }
        try {
            const engine = ($('select-engine-mode') || {}).value || 'recap';
            await api('/api/profiles', { method: 'POST', body: JSON.stringify({ name, engine_mode: engine, options: profileOptionsFromWizard() }) });
            toast(t('bkSaved'), 'success');
            $('bk-name').value = '';
            loadProfiles();
        } catch (e) {
            toast(t('bkFailed') + ': ' + e.message, 'error');
        }
    }

    async function deleteProfile(profile) {
        if (!window.confirm(t('bkConfirmDelete', { name: profile.name }))) return;
        try {
            await api('/api/profiles/' + encodeURIComponent(profile.id), { method: 'DELETE' });
            toast(t('bkDeleted'));
            loadProfiles();
        } catch (e) {
            toast(t('bkFailed') + ': ' + e.message, 'error');
        }
    }

    // How each option is written back into the wizard (the inverse of OPTION_READERS in index.html).
    const setValue = (id, value) => { const node = $(id); if (node && value !== null && value !== undefined) node.value = String(value); };
    const setChecked = (id, value) => {
        const node = $(id);
        if (!node) return;
        node.checked = Boolean(value);
        node.dispatchEvent(new Event('change', { bubbles: true }));
    };
    /** Selects whose options are numbers (opacity, volume...): pick the closest available one. */
    function setNearest(id, value) {
        const node = $(id);
        if (!node || typeof value !== 'number') return;
        let best = null;
        for (const option of node.options) {
            const distance = Math.abs(parseFloat(option.value) - value);
            if (!Number.isNaN(distance) && (best === null || distance < best.distance)) best = { value: option.value, distance };
        }
        if (best) node.value = best.value;
    }
    const call = (name, ...args) => { if (typeof window[name] === 'function') window[name](...args); };

    const OPTION_WRITERS = {
        source_language: v => setValue('select-source-lang', v),
        language: v => setValue('select-lang', v),
        translation_style: v => setValue('select-translation-style', v),
        story_length: v => setValue('select-story-length', v),
        narrative_tone: v => setValue('select-narrative-tone', v),
        tts_rate: v => setValue('select-tts-rate', v === null || v === undefined ? '' : v),
        resolution: v => setValue('select-resolution', v),
        video_format: v => setValue('select-video-format', v),
        subtitle_style: v => call('selectSubPreset', v),
        subtitle_mode: v => setValue('select-hardsub-mode', v),
        render_video: v => setValue('select-render-video', String(v)),
        delivery_mode: v => {
            const radio = document.querySelector('input[name="delivery_mode"][value="' + v + '"]');
            if (radio) { radio.checked = true; call('handleDeliveryChange'); }
        },
        watermark_enabled: v => { setChecked('chk-watermark', v); call('toggleWatermarkControls', Boolean(v)); },
        watermark_text: v => setValue('input-watermark-text', v),
        watermark_opacity: v => setNearest('select-watermark-opacity', v),
        blur_mode: v => { setValue('select-blur-mode', v); call('handleBlurModeChange', v); },
        blur_height: v => setNearest('select-blur-height', v),
        mirror: v => setChecked('chk-mirror', v),
        color_grading: v => setChecked('chk-color-grading', v),
        audio_anti_copyright: v => setChecked('chk-audio-anti-copyright', v),
        tts_engine: v => setValue('select-tts-engine', v),
        audio_mode: v => { setValue('select-audio-mode', v); call('toggleAudioModeControls', v); },
        sfx_mode: v => setValue('select-sfx-mode', v),
        sfx_volume: v => setNearest('select-sfx-volume', v),
        stage_toggles: v => {
            if (!v || typeof v !== 'object') return;
            Object.entries(v).forEach(([key, on]) => { const node = $('stage-' + key); if (node) node.checked = Boolean(on); });
            call('updateStageBadge');
        }
    };

    function applyProfile(profile) {
        const engineSelect = $('select-engine-mode');
        if (engineSelect) { engineSelect.value = profile.engine_mode; call('handleEngineChange', profile.engine_mode); }   // engine defaults first
        for (const [name, value] of Object.entries(profile.options || {})) {
            const write = OPTION_WRITERS[name];
            if (write) { try { write(value); } catch (e) { console.warn('Profile option not applied:', name, e); } }
        }
        toast(t('bkApplied', { name: profile.name }), 'success');
        window.switchView('studio');
        window.goToStep(1);
    }


    // ── pronunciation dictionary ──────────────────────────────────────────────
    async function loadDictionary() {
        try {
            const data = await api('/api/dictionary');
            $('dict-text').value = data.text || '';
            $('dict-count').textContent = String(data.count || 0);
        } catch (e) {
            toast(t('dictFailed') + ': ' + e.message, 'error');
        }
    }

    async function saveDictionary() {
        const button = $('dict-save');
        button.disabled = true;
        try {
            const data = await api('/api/dictionary', { method: 'PUT', body: JSON.stringify({ text: $('dict-text').value }) });
            $('dict-text').value = data.text || '';
            $('dict-count').textContent = String(data.count || 0);
            toast(t('dictSaved', { n: data.count }), 'success');
        } catch (e) {
            toast(t('dictFailed') + ': ' + e.message, 'error');
        } finally {
            button.disabled = false;
        }
    }

    // ── Gemini key test (nothing is saved) ────────────────────────────────────
    const KEY_STATE_LABEL = { 'ONLINE': 'keyOnline', 'INVALID KEY': 'keyInvalid', 'RATE LIMITED (429)': 'keyRate', 'UNREACHABLE': 'keyUnreachable' };

    async function checkKeys() {
        const area = $('key-editor-textarea');
        const box = $('key-check-result');
        const button = $('btn-check-keys');
        const lines = area.value.split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
        if (!lines.length) { toast(t('keyNoKeys'), 'warn'); area.focus(); return; }
        button.disabled = true;
        box.classList.remove('hidden');
        box.replaceChildren(el('span', { class: 'spinner' }), el('span', { class: 'muted', text: t('keyChecking') }));
        try {
            const data = await api('/api/keys/check', { method: 'POST', body: JSON.stringify({ keys: lines }) });
            const results = data.results || [];
            box.replaceChildren(...results.map(r => el('span', { class: 'chip ' + (r.ok ? 'chip-ok' : (r.http_code === 429 ? 'chip-warn' : 'chip-err')) },
                r.key + ' · ' + (KEY_STATE_LABEL[r.state] ? t(KEY_STATE_LABEL[r.state]) : r.state))));
            const bad = results.filter(r => !r.ok && r.http_code !== 429).length;
            toast(bad ? t('keySomeBad', { bad, n: results.length }) : t('keyAllGood', { n: results.length }), bad ? 'error' : 'success');
        } catch (e) {
            box.replaceChildren(el('span', { class: 'chip chip-err', text: t('keyFailed') + ': ' + e.message }));
        } finally {
            button.disabled = false;
        }
    }


    // ── view lifecycle ────────────────────────────────────────────────────────
    function onShowView(view) {
        if (state.view === 'editor' && view !== 'editor' && state.editor.dirty.size && !window.confirm(t('seUnsaved'))) {
            // Leaving with unsaved edits was declined: stay on the editor.
            window.switchView('editor');
            return;
        }
        state.view = view;
        if (view === 'projects') { showProjectsTab(state.projectsTab); refreshProjects(); renderProjects(); }
        if (view === 'brand') { loadProfiles(); loadDictionary(); }
        if (view === 'studio' || view === 'outputs') loadStorage();
    }

    function bindShortcuts() {
        document.addEventListener('keydown', event => {
            const target = event.target;
            const typing = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable);
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && state.view === 'editor') {
                event.preventDefault();
                saveScript(false);
            } else if (!typing && !event.ctrlKey && !event.metaKey && !event.altKey && event.key.toLowerCase() === 't') {
                toggleTheme();
            } else if (!typing && event.key === '/' && state.view === 'editor') {
                event.preventDefault();
                $('se-search').focus();
            } else if (event.key === 'Escape') {
                closeQuality();
            }
        });
    }

    function init() {
        const toggle = $('theme-toggle');
        if (toggle) toggle.addEventListener('click', toggleTheme);
        bindEditor();
        bindShortcuts();
        applyLanguage();
        loadStorage();
        loadProjects();     // fills the review badge in the navigation
    }

    window.PaiStudio = {
        ui: { el, api, toast, clock, lang, formatDate },          // shared with static/js/tools.js
        applyLanguage, viewTitle, onShowView, t,
        showProjectsTab, refreshProjects, loadProjects,
        openEditor, saveScript, closeQuality, openQuality,
        saveProfileFromWizard, applyProfile, checkKeys, saveDictionary, previewReplace, applyReplace, exportCapcut
    };

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
