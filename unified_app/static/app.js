let allQuestions = [];
let activeFilter = 'all';
let selectedFilename = null;
let selectedAnsFilename = null;
let activeJobId = null;
let expectedDiagrams = [];
let jobPollTimer = null;
let watchPoll = null;
let watchActive = false;
let jobStartTime = null;
let jobPanelCollapsed = false;

const ALL_STEPS = ['extract', 'diagrams', 'reword', 'solve', 'validate', 'fixing', 'regen_diagrams', 'completed'];

const STEP_LABELS = {
  extract:        { running: '⚙️ 1. Extracting from PDF', done: '✅ 1. Extraction Complete' },
  diagrams:       { running: '📸 2. Capturing Diagrams', done: '✅ 2. Diagrams Captured' },
  reword:         { running: '⚡ 3. Rewording (KaTeX Normalization)', done: '✅ 3. Rewording Complete' },
  solve:          { running: '🧮 4. Solving (First Principles)', done: '✅ 4. Solved' },
  validate:       { running: '✅ 5. Validating Answers', done: '✅ 5. Validated' },
  fixing:         { running: '🔧 6. Fixing & Rechecking Flags', done: '✅ 6. Flags Resolved' },
  regen_diagrams: { running: '🎨 7. Regenerating & Validating Diagrams', done: '✅ 7. Diagrams Regenerated' },
  completed:      { running: '📦 8. Finalizing', done: '✅ 8. Pipeline Completed' },
};

function statusToStep(status){
  if(status==='extracting' || status==='extraction_failed') return 'extract';
  if(status==='awaiting_screenshots' || status==='diagrams_uploaded') return 'diagrams';
  if(status==='rewording' || status==='reworded') return 'reword';
  if(status==='solving') return 'solve';
  if(status==='validating') return 'validate';
  if(status==='fixing' || status==='rechecking' || status==='rechecked') return 'fixing';
  if(status==='regen_diagrams' || status==='validating_diagrams' || status==='fixing_diagrams' || status==='diagrams_regenerated') return 'regen_diagrams';
  if(status==='completed') return 'completed';
  if(status==='failed') return 'extract';
  return 'extract';
}

function stepIndex(step){ return ALL_STEPS.indexOf(step); }

document.addEventListener('DOMContentLoaded', () => {
  const saved = localStorage.getItem('activeJobId');
  if(saved) activeJobId = saved;
  initUpload();
  initActions();
  fetchQuestions();
  fetchJobs();
  initTelemetry();
  populateProviderSelect();
  if(activeJobId) restoreJob(activeJobId);
  setInterval(fetchJobs, 4000);
  document.getElementById('btnJobPanelCollapse')?.addEventListener('click', toggleJobPanel);
  document.querySelectorAll('.step-dot').forEach(el=> el.addEventListener('click', ()=>{
    const s=el.dataset.step;
    const cur=statusToStep(document.getElementById('jobPanel')?.dataset.status||'extract');
    if(stepIndex(s) <= stepIndex(cur)){
      document.getElementById('jobPanelBody')?.scrollIntoView({behavior:'smooth', block:'start'});
    }
  }));
});

async function restoreJob(jobId){
  try{
    const r=await fetch(`/api/status/${jobId}`);
    const j=await r.json();
    if(j.error) { localStorage.removeItem('activeJobId'); activeJobId=null; return; }
    expectedDiagrams=j.expected_diagrams||[];
    renderJobPanel(jobId, j.status, expectedDiagrams, j);
    renderJobStepper(jobId, j.status, expectedDiagrams);
    if(expectedDiagrams.length || j.status==='awaiting_screenshots') renderDiagramFinder(jobId, expectedDiagrams);
    startJobPolling();
    if(watchPoll) clearInterval(watchPoll);
    watchPoll=setInterval(fetchWatchStatus,1200);
    fetchWatchStatus();
  }catch(e){}
}

function toggleJobPanel(){
  jobPanelCollapsed=!jobPanelCollapsed;
  const body=document.getElementById('jobPanelBody');
  const hist=document.getElementById('jobPanelHistory');
  const btn=document.getElementById('btnJobPanelCollapse');
  if(jobPanelCollapsed){ body.style.display='none'; if(hist) hist.style.display='none'; btn.textContent='▸ expand'; }
  else { body.style.display='block'; if(hist && hist.innerHTML) hist.style.display='block'; btn.textContent='▾ collapse'; }
}

function setActiveJob(id){
  activeJobId=id;
  if(id) localStorage.setItem('activeJobId', id);
  else localStorage.removeItem('activeJobId');
  jobStartTime=Date.now();
}

function startJobPolling(){
  if(jobPollTimer) clearInterval(jobPollTimer);
  jobPollTimer=setInterval(async()=>{
    if(!activeJobId) return;
    try{
      const r=await fetch(`/api/status/${activeJobId}`);
      const j=await r.json();
      if(j.error) return;
      expectedDiagrams=j.expected_diagrams||[];
      renderJobPanel(activeJobId, j.status, expectedDiagrams, j);
      renderJobStepper(activeJobId, j.status, expectedDiagrams);
      if(j.status!=='awaiting_screenshots' && watchPoll && j.status!=='diagrams_uploaded'){
        clearInterval(watchPoll); watchPoll=null;
      }
      if(j.status==='awaiting_screenshots' || j.status==='diagrams_uploaded') {
        renderDiagramFinder(activeJobId, expectedDiagrams);
      } else {
        const diagSec = document.getElementById('diagramFinderSection');
        if(diagSec && j.status==='completed') diagSec.style.display = 'none';
      }
      if(['reworded','solving','validating','fixing','rechecking','rechecked','completed','diagrams_uploaded'].includes(j.status)){
        fetchQuestions();
      }
      if(j.status==='failed' || j.status==='extraction_failed'){
        showInterstitial(`❌ Job failed: ${j.error||'unknown error'} — raw saved to output/debug_*.txt`, '#ef4444');
      }
    }catch(e){}
  },1200);
}

async function populateProviderSelect(){
  const sel=document.getElementById('providerSelect');
  if(!sel) return;
  try{
    const res=await fetch('/api/profiles');
    const data=await res.json();
    const profiles=data.profiles||[];
    const avail=profiles.filter(p=>p.authenticated && !p.disabled);
    sel.innerHTML='';
    const autoOpt=document.createElement('option');
    autoOpt.value='auto'; autoOpt.textContent='Auto (Balanced DB Pool - All Accounts)';
    sel.appendChild(autoOpt);
    avail.forEach(p=>{
      const o=document.createElement('option');
      o.value=p.name;
      o.textContent=`${p.name} (${p.email||'no email'})`;
      sel.appendChild(o);
    });
    if(!avail.length){
      const o=document.createElement('option');
      o.value='simulation'; o.textContent='Local Simulation Parser';
      sel.appendChild(o);
    }
  }catch(e){
    sel.innerHTML='<option value="auto">Auto (Balanced DB Pool)</option>';
  }
}

function initUpload() {
  const uploadZone = document.getElementById('uploadZone');
  const fileInput = document.getElementById('fileInput');
  const fileSelectedName = document.getElementById('fileSelectedName');
  const ansUploadZone = document.getElementById('ansUploadZone');
  const ansFileInput = document.getElementById('ansFileInput');
  const ansFileSelectedName = document.getElementById('ansFileSelectedName');
  const btnStart = document.getElementById('btnStartExtraction');
  uploadZone.addEventListener('dragover', (e) => { e.preventDefault(); uploadZone.style.borderColor = '#6366f1'; });
  uploadZone.addEventListener('dragleave', () => { uploadZone.style.borderColor = ''; });
  uploadZone.addEventListener('drop', (e) => {
    e.preventDefault(); uploadZone.style.borderColor = '';
    if (e.dataTransfer.files && e.dataTransfer.files.length) handleFileUpload(e.dataTransfer.files[0], 'question');
  });
  fileInput.addEventListener('change', () => { if (fileInput.files && fileInput.files.length) handleFileUpload(fileInput.files[0], 'question'); });
  if (ansUploadZone) {
    ansUploadZone.addEventListener('dragover', (e) => { e.preventDefault(); ansUploadZone.style.borderColor = '#10b981'; });
    ansUploadZone.addEventListener('dragleave', () => { ansUploadZone.style.borderColor = ''; });
    ansUploadZone.addEventListener('drop', (e) => {
      e.preventDefault(); ansUploadZone.style.borderColor = '';
      if (e.dataTransfer.files && e.dataTransfer.files.length) handleFileUpload(e.dataTransfer.files[0], 'answer');
    });
    if (ansFileInput) ansFileInput.addEventListener('change', () => { if (ansFileInput.files && ansFileInput.files.length) handleFileUpload(ansFileInput.files[0], 'answer'); });
  }
  const uploadPrompt = document.getElementById('uploadPrompt');
  const ansUploadPrompt = document.getElementById('ansUploadPrompt');
  async function handleFileUpload(file, type) {
    const formData = new FormData(); formData.append('file', file);
    const targetLabel = type === 'question' ? fileSelectedName : ansFileSelectedName;
    try {
      targetLabel.textContent = 'Uploading ' + file.name + '...';
      const res = await fetch('/api/upload', { method: 'POST', body: formData });
      const data = await res.json();
      if (data.ok) {
        if (type === 'question') {
          selectedFilename = data.filename;
          targetLabel.textContent = '✅ Ready: ' + data.filename;
          btnStart.disabled = false;
          if (uploadZone) { uploadZone.style.borderColor = '#10b981'; uploadZone.style.backgroundColor = 'rgba(16, 185, 129, 0.08)'; }
          if (uploadPrompt) uploadPrompt.innerHTML = `<span class="upload-icon" style="color:#10b981;">✅</span><p style="color:#10b981; font-weight:600; font-size:0.95rem;">${escapeHtml(data.filename)}</p><span class="file-hint" style="color:#9ca3af;">Click or drag to replace file</span>`;
        } else {
          selectedAnsFilename = data.filename;
          targetLabel.textContent = '✅ Answer Key: ' + data.filename;
          if (ansUploadZone) { ansUploadZone.style.borderColor = '#10b981'; ansUploadZone.style.backgroundColor = 'rgba(16, 185, 129, 0.08)'; }
          if (ansUploadPrompt) ansUploadPrompt.innerHTML = `<span class="upload-icon" style="color:#10b981; font-size:1.3rem;">✅</span><p style="color:#10b981; font-weight:600; font-size:0.85rem;">${escapeHtml(data.filename)}</p><span class="file-hint" style="color:#9ca3af;">Click or drag to replace answer key</span>`;
        }
      } else targetLabel.textContent = 'Error: ' + (data.error || 'Upload failed');
    } catch (err) { targetLabel.textContent = 'Upload failed: ' + err.message; }
  }
  btnStart.addEventListener('click', async () => {
    if (!selectedFilename) return;
    const provider = document.getElementById('providerSelect').value;
    btnStart.disabled = true; btnStart.textContent = '⏳ Dispatching...';
    try {
      const res = await fetch('/api/extract', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: selectedFilename, answer_key_filename: selectedAnsFilename, provider: provider }),
      });
      const data = await res.json();
      if (data.ok) {
        setActiveJob(data.job_id);
        expectedDiagrams = [];
        renderJobPanel(data.job_id, 'extracting', [], {progress:{stage:'extracting', pct:5, msg:'Queued extraction...'}});
        renderJobStepper(data.job_id, 'extracting', []);
        fetchJobs();
        startJobPolling();
        showInterstitial('⏳ Extraction started — PDF is being parsed in background across multi-account pool.', '#6366f1');
        if(watchPoll) clearInterval(watchPoll);
        watchPoll=setInterval(fetchWatchStatus, 1200);
      } else alert(data.error||'Failed');
    } catch (err) { alert('Failed to start extraction: ' + err.message); }
    finally { btnStart.disabled = false; btnStart.textContent = '⚡ Start Parallel Extraction & Structuring'; }
  });
}

function showInterstitial(msg, color){
  const el=document.getElementById('extractionInterstitial');
  if(!el) return;
  el.textContent=msg;
  el.style.display='block';
  el.style.borderColor=color||'#334155';
  el.style.background=color==='ok'?'rgba(16,185,129,0.08)':'#0f172a';
}
function hideInterstitial(){ const el=document.getElementById('extractionInterstitial'); if(el) el.style.display='none'; }

function renderJobPanel(jobId, status, diagrams, fullJob){
  const panel=document.getElementById('jobPanel');
  if(!panel) return;
  panel.style.display='block';
  panel.dataset.status=status;
  const fileEl=document.getElementById('jobPanelFile');
  const timerEl=document.getElementById('jobPanelTimer');
  const progressEl=document.getElementById('jobPanelProgress');
  const progressText=document.getElementById('jobPanelProgressText');
  const statPct=document.getElementById('jobPanelStatPct');
  const stageBadge=document.getElementById('jobPanelStageBadge');
  const statusLabel=document.getElementById('jobPanelStatusLabel');
  const body=document.getElementById('jobPanelBody');
  const history=document.getElementById('jobPanelHistory');
  const accountsEl=document.getElementById('jobPanelAccounts');

  if(fileEl) fileEl.textContent=`📄 ${jobId}`;
  if(timerEl && jobStartTime){ const s=Math.floor((Date.now()-jobStartTime)/1000); timerEl.textContent=`⏱ ${s}s`; }
  
  // Percentage clamp 0..100
  const rawPct = fullJob?.progress?.pct ?? fullJob?.percentage ?? 0;
  const pct = Math.max(0, Math.min(100, Math.round(rawPct)));
  const msg = fullJob?.progress?.msg || '';
  
  if(progressEl) progressEl.style.width=`${pct}%`;
  if(progressText) progressText.textContent = `${pct}% · ${msg||status} ${diagrams.length?`· ${diagrams.filter(d=>d.fulfilled).length}/${diagrams.length} diagrams`:''}`;
  if(statPct) statPct.textContent = `${pct}%`;

  const step=statusToStep(status);
  ALL_STEPS.forEach(s=>{
    const el=document.getElementById('jpStep'+s.charAt(0).toUpperCase()+s.slice(1));
    if(!el) return;
    el.className='step-dot';
    if(s===step) el.classList.add('active');
    else if(stepIndex(s) < stepIndex(step)) el.classList.add('done');
  });

  const labels={
    extracting: '⚙️ 1. Extracting PDF',
    extraction_failed: '⚠️ Extraction Failed',
    awaiting_screenshots: '📸 2. Capturing Diagrams',
    diagrams_uploaded: '✅ 2. Diagrams Ready',
    rewording: '⚡ 3. Rewording (KaTeX)',
    reworded: '⚡ 3. Reworded',
    solving: '🧮 4. Solving from First Principles',
    validating: '✅ 5. Validating Answers',
    fixing: '🔧 6. Fixing Flags',
    rechecking: '🔍 6. Rechecking',
    rechecked: '🔍 6. Recheck Finished',
    completed: '✅ 7. Completed',
    failed: '❌ Failed',
  };
  if(statusLabel) statusLabel.textContent=labels[status]||status;
  if(stageBadge) stageBadge.textContent=labels[status]||status;

  // Body rendering
  let html='';
  if(status==='extracting'){
    html=`<div style="text-align:center;padding:0.75rem;"><div class="pulse-indicator" style="display:inline-block"></div> <strong>${STEP_LABELS.extract.running}</strong><br><span style="font-size:0.8rem;color:#94a3b8;">${msg||'Parsing document & extracting subparts in parallel across DB pool...'}</span></div>`;
    hideInterstitial();
  } else if(status==='awaiting_screenshots'){
    const done=diagrams.filter(d=>d.fulfilled).length;
    const total=diagrams.length;
    html=`<div style="margin-bottom:0.6rem; background:${total&&done===total?'rgba(16,185,129,0.08)':'rgba(99,102,241,0.08)'}; border:1px solid ${total&&done===total?'#10b981':'#6366f1'}; padding:0.6rem 0.8rem; border-radius:8px; text-align:center;">✅ ${fullJob?.count||'?'} questions extracted — ${total?`${total} diagrams expected in FIFO queue.`:'No diagrams needed — ready to reword'}<br><strong>${done}/${total} captured</strong> ${total&&done===total?'— ready to reword':''}</div>`;
    html+=`<div style="display:flex;gap:0.5rem;flex-wrap:wrap;justify-content:center;margin-bottom:0.6rem;">
      <button id="jpBtnToggleWatch" class="btn ${watchActive?'btn-warning':'btn-primary'}" style="font-size:0.8rem;">${watchActive?'⏸ Stop Auto-Capture':'▶ Start Auto-Capture'}</button>
      <button id="jpBtnUndo" class="btn btn-secondary" style="font-size:0.8rem;">↩ Undo Last Screenshot</button>
      <button id="jpBtnPick" class="btn btn-secondary" style="font-size:0.8rem;">📎 Manual Assign</button>
    </div>`;
    html+=`<div id="jpDiagramList" style="display:flex;flex-direction:column;gap:0.3rem;max-height:260px;overflow:auto;">${diagrams.map((d,i)=>{
      const doneFlag=!!d.fulfilled; const isNext=!doneFlag && diagrams.findIndex(x=>!x.fulfilled)===i;
      const undoBtn = doneFlag ? `<button class="btn btn-secondary" style="font-size:0.68rem;padding:0.15rem 0.4rem;margin-left:0.4rem;" onclick="undoDiagramItem(${i})">↩ Undo</button>` : '';
      return `<div style="display:flex;justify-content:space-between;align-items:center;background:${doneFlag?'#064e3b':isNext?'#1e1b4b':'#0f172a'};padding:0.35rem 0.5rem;border-radius:6px;border:1px solid ${doneFlag?'#10b981':isNext?'#6366f1':'#334155'};font-size:0.77rem;"><span><strong>Q${d.question_number}</strong> ${d.label_path?`· ${escapeHtml(d.label_path)}`:''} · ${escapeHtml(d.part)} p${d.page||'?'}${isNext?' <span style="color:#818cf8;font-weight:700;">← NEXT</span>':''}<br><span style="color:${doneFlag?'#6ee7b7':'#94a3b8'}">${escapeHtml(d.expected_name)} ${doneFlag?'✅':''}</span></span><div style="display:flex;align-items:center;"><span style="font-size:0.7rem;color:${doneFlag?'#10b981':isNext?'#818cf8':'#64748b'}">${doneFlag?'done':isNext?'next':'queued'}</span>${undoBtn}</div></div>`;
    }).join('')}</div>`;
    if(total&&done===total) showInterstitial(`✅ All ${total} diagrams captured — ready to reword`, '#10b981');
    else showInterstitial(`🎯 Now Capturing: ${diagrams.find(d=>!d.fulfilled)?.expected_name||'—'}`, '#6366f1');
  } else if(status==='diagrams_uploaded'){
    html=`<div style="text-align:center;padding:0.6rem;background:rgba(16,185,129,0.08);border:1px solid #10b981;border-radius:8px;">✅ All diagrams captured — ready to start full automated pipeline<br><button id="jpBtnReword" class="btn btn-primary" style="margin-top:0.5rem;font-size:0.85rem;">⚡ Run Full Pipeline (Reword → Solve → Validate → Fix)</button></div>`;
    showInterstitial(`✅ ${fullJob?.count||'?'} questions extracted — All diagrams captured — ready to run pipeline`, '#10b981');
  } else if(status==='rewording'){
    html=`<div style="text-align:center;padding:0.6rem;"><span class="pulse-indicator" style="display:inline-block"></span> <strong>⚡ Step 3: Rewording & KaTeX Normalization</strong><br><span style="font-size:0.78rem;color:#94a3b8;">Processing 10-question bundles across balanced accounts pool</span></div>`;
  } else if(status==='solving'){
    html='<div style="text-align:center;padding:0.6rem;"><span class="pulse-indicator" style="display:inline-block"></span> <strong>🧮 Step 4: Solving Questions from First Principles</strong><br><span style="font-size:0.78rem;color:#94a3b8;">Generating step-by-step student derivations in parallel</span></div>';
  } else if(status==='validating'){
    html='<div style="text-align:center;padding:0.6rem;"><span class="pulse-indicator" style="display:inline-block"></span> <strong>✅ Step 5: Validating Answers Independently</strong><br><span style="font-size:0.78rem;color:#94a3b8;">Independent cross-verification without seeing previous solution</span></div>';
  } else if(status==='fixing' || status==='rechecking'){
    html='<div style="text-align:center;padding:0.6rem;"><span class="pulse-indicator" style="display:inline-block"></span> <strong>🔧 Step 6: Resolving Mismatches & KaTeX Flags</strong><br><span style="font-size:0.78rem;color:#94a3b8;">Refining divergence and executing secondary independent verification</span></div>';
  } else if(status==='completed'){
    const validCount = fullJob?.valid || 0;
    const hrCount = fullJob?.human_review_count || fullJob?.still_flagged || 0;
    html=`<div style="text-align:center;padding:0.75rem;background:rgba(16,185,129,0.08);border:1px solid #10b981;border-radius:8px;">
      <strong>✅ Step 7: Full Pipeline Completed!</strong><br>
      <span style="font-size:0.82rem; color:#a7f3d0;">${validCount} Fully Verified Questions · ${hrCount} Require Human Review</span><br>
      <div style="display:flex; justify-content:center; gap:0.6rem; margin-top:0.6rem;">
        <button id="jpBtnExportFinal" class="btn btn-primary" style="font-size:0.85rem;">📦 Export Final JSON</button>
        <button id="jpBtnExportAll" class="btn btn-secondary" style="font-size:0.85rem;">📥 Export All</button>
      </div>
    </div>`;
  } else if(status==='failed' || status==='extraction_failed'){
    html=`<div style="text-align:center;padding:0.7rem;background:rgba(239,68,68,0.08);border:1px solid #ef4444;border-radius:8px;color:#fca5a5;">⚠️ Processing Failed<br><span style="font-size:0.78rem;">${escapeHtml(fullJob?.error||'Raw output saved for review')}</span><br><button class="btn btn-secondary" style="margin-top:0.5rem;font-size:0.8rem;" onclick="location.reload()">🔄 Retry</button></div>`;
  }

  const prevList=document.getElementById('jpDiagramList');
  const prevScroll=prevList ? prevList.scrollTop : 0;
  body.innerHTML=html;
  const newList=document.getElementById('jpDiagramList');
  if(newList) newList.scrollTop=prevScroll;

  // Wire action buttons
  document.getElementById('jpBtnToggleWatch')?.addEventListener('click', toggleWatch);
  document.getElementById('jpBtnUndo')?.addEventListener('click', undoLast);
  document.getElementById('jpBtnPick')?.addEventListener('click', ()=> document.getElementById('diagramFileInput')?.click());
  document.getElementById('jpBtnReword')?.addEventListener('click', async()=>{
    const b=document.getElementById('jpBtnReword'); b.disabled=true; b.textContent='⏳ Running Pipeline...';
    try{ await runReword(jobId); }catch(e){alert(e.message);} finally{ b.disabled=false; b.textContent='⚡ Run Full Pipeline'; }
  });
  document.getElementById('jpBtnExportAll')?.addEventListener('click', ()=> window.location.href='/api/export');
  document.getElementById('jpBtnExportFinal')?.addEventListener('click', ()=> window.location.href=`/api/export_final/${jobId}`);

  // Accounts strip
  if(accountsEl){
    fetch('/api/telemetry').then(r=>r.json()).then(t=>{
      if(t.accounts){
        accountsEl.innerHTML=t.accounts.map(acc=>{
          const state=acc.state||'idle';
          const countInfo = acc.total_batches ? ` [${acc.total_batches}b]` : '';
          return `<div class="account-chip state-${state}" title="${escapeHtml(acc.email||acc.name)} (total batches: ${acc.total_batches||0})"><span class="chip-dot"></span><span>${escapeHtml(acc.name)}${countInfo}</span></div>`;
        }).join('');
      }
      const ticker = document.getElementById('jobPanelTicker');
      if(ticker && t.events && t.events.length){
        const last = t.events[t.events.length - 1];
        ticker.textContent = `[${last.time}] ${last.msg}`;
      }
    });
  }

  const expFinalBtn = document.getElementById('btnExportFinal');
  if(expFinalBtn) expFinalBtn.style.display = (status==='completed') ? 'inline-block' : 'none';
}

function renderJobStepper(jobId, status, diagrams){
  const expFinal = document.getElementById('btnExportFinal');
  if (expFinal) expFinal.style.display = (status==='completed') ? 'inline-block' : 'none';
}

function renderDiagramFinder(jobId, diagrams){
  const sec=document.getElementById('diagramFinderSection');
  const list=document.getElementById('diagramChecklist');
  if(!sec || !list) return;
  expectedDiagrams=diagrams;
  
  if(!diagrams.length){
    sec.style.display='block';
    list.innerHTML='<p style="color:#9ca3af; font-size:0.8rem;">No diagrams expected — ready to reword.</p>';
    const c=document.getElementById('nowCapturingCard'); if(c) c.style.display='none';
    return;
  }
  sec.style.display='block';
  list.innerHTML=diagrams.map((d,i)=>{
    const done=!!d.fulfilled; const isNext=!done && diagrams.findIndex(x=>!x.fulfilled)===i;
    const undoBtn = done ? `<button class="btn btn-secondary" style="font-size:0.7rem; padding:0.2rem 0.5rem; margin-left:0.5rem;" onclick="undoDiagramItem(${i})">↩ Undo</button>` : '';
    return `<div style="display:flex; justify-content:space-between; align-items:center; background:${done?'#064e3b':isNext?'#1e1b4b':'#0f172a'}; padding:0.45rem 0.7rem; border-radius:6px; border:1px solid ${done?'#10b981':isNext?'#6366f1':'#334155'};">
      <div style="font-size:0.8rem;">
        <strong>Q${d.question_number}</strong> ${d.label_path?`· ${escapeHtml(d.label_path)}`:''} · ${escapeHtml(d.part)}${d.order?` #${d.order}`:''} · p${d.page||'?'}${isNext?' <span style="color:#818cf8; font-weight:700;">← NEXT</span>':''}<br>
        <span style="color:${done?'#6ee7b7':'#94a3b8'};">${escapeHtml(d.expected_name)} ${done?'✅':''}</span><br>
        <span style="color:#64748b; font-size:0.75rem;">hint: ${escapeHtml(d.hint||'')}</span>
      </div>
      <div style="display:flex; align-items:center;">
        <span style="font-size:0.72rem; color:${done?'#10b981':isNext?'#818cf8':'#64748b'};">${done?'done':isNext?'next':'queued'}</span>
        ${undoBtn}
      </div>
    </div>`;
  }).join('');
  const doneCount=diagrams.filter(d=>d.fulfilled).length;
  const statusEl=document.getElementById('diagramUploadStatus');
  if(statusEl) statusEl.textContent=`${doneCount}/${diagrams.length} captured${doneCount===diagrams.length?' — ready to reword':''}`;
  renderNowCapturing(diagrams);
}

function renderNowCapturing(diagrams){
  const card=document.getElementById('nowCapturingCard');
  const prog=document.getElementById('nowCapturingProgress');
  const label=document.getElementById('nowCapturingLabel');
  const hint=document.getElementById('nowCapturingHint');
  const nameEl=document.getElementById('nowCapturingName');
  if(!card||!diagrams) return;
  const total=diagrams.length; const fulfilled=diagrams.filter(d=>d.fulfilled).length; const nxt=diagrams.find(d=>!d.fulfilled);
  if(!nxt){ card.style.display='block'; card.style.borderColor='#10b981'; prog.textContent=`${fulfilled} of ${total} — All captured ✓`; label.textContent='Ready to Reword'; hint.textContent=''; nameEl.textContent=''; return; }
  const idx=diagrams.indexOf(nxt)+1;
  card.style.display='block'; card.style.borderColor=watchActive?'#6366f1':'#334155';
  prog.textContent=`NOW CAPTURING (${idx} of ${total}) ${watchActive?'● watching':''}`;
  label.textContent=`Q${nxt.question_number}${nxt.label_path?' '+nxt.label_path:''} · ${nxt.part==='question'?'Question Diagram':`Answer Diagram ${nxt.part}`} · Page ${nxt.page||'?'}`;
  hint.textContent=nxt.hint?`"${nxt.hint}"`:'';
  nameEl.textContent=nxt.expected_name;
}

async function fetchWatchStatus(){
  if(!activeJobId) return;
  try{
    const res=await fetch(`/api/diagram_watch/status/${activeJobId}`);
    const data=await res.json();
    if(data.error) return;
    const stRes=await fetch(`/api/status/${activeJobId}`);
    const j=await stRes.json();
    if(j.expected_diagrams) expectedDiagrams=j.expected_diagrams;
    watchActive=!!data.active;
    updateWatchUI(data);
    renderDiagramFinder(activeJobId, expectedDiagrams);
    renderJobPanel(activeJobId, j.status||data.status, expectedDiagrams, j);
  }catch(e){}
}

function updateWatchUI(s){
  const btn=document.getElementById('btnToggleWatch');
  const txt=document.getElementById('watchStatusText');
  const undo=document.getElementById('btnUndoCapture');
  const disc=document.getElementById('btnDiscardCapture');
  watchActive=!!s.active;
  if(btn) { btn.textContent=watchActive?'⏸ Stop Auto-Capture':'▶ Start Auto-Capture'; btn.className=watchActive?'btn btn-warning':'btn btn-primary'; }
  if(txt) txt.textContent=watchActive?`Watching ${s.watch_dir} — ${s.fulfilled}/${s.total}`:`Stopped — ${s.fulfilled}/${s.total}`;
  if(undo) undo.style.display=s.fulfilled>0 ? 'inline-block' : 'none';
  if(disc) disc.style.display=watchActive?'inline-block':'none';
  if(s.last_captured && txt) txt.textContent+=` · last: ${s.last_captured.dest}`;
  const jpBtn=document.getElementById('jpBtnToggleWatch');
  if(jpBtn){ jpBtn.textContent=watchActive?'⏸ Stop Auto-Capture':'▶ Start Auto-Capture'; jpBtn.className=watchActive?'btn btn-warning':'btn btn-primary'; }
}

async function toggleWatch(){
  if(!activeJobId) return alert('No active job');
  const btn=document.getElementById('btnToggleWatch')||document.getElementById('jpBtnToggleWatch');
  if(btn) btn.disabled=true;
  try{
    if(watchActive) await fetch(`/api/diagram_watch/stop/${activeJobId}`,{method:'POST'});
    else { const res=await fetch(`/api/diagram_watch/start/${activeJobId}`,{method:'POST'}); const d=await res.json(); if(!d.ok) alert(d.error||'start failed'); }
    await fetchWatchStatus();
  }finally{ if(btn) btn.disabled=false; }
}

async function undoLast(){
  if(!activeJobId) return;
  try {
    const res = await fetch(`/api/diagram_watch/undo/${activeJobId}`,{method:'POST'});
    const data = await res.json();
    if(data.ok) {
      await fetchWatchStatus();
    } else alert('Undo failed: ' + (data.error || 'unknown'));
  } catch(e) { alert('Undo error: ' + e.message); }
}

async function undoDiagramItem(index){
  if(!activeJobId) return;
  try {
    const res = await fetch(`/api/diagram_watch/undo_item/${activeJobId}/${index}`, {method:'POST'});
    const data = await res.json();
    if(data.ok) {
      await fetchWatchStatus();
    } else alert('Undo item failed: ' + (data.error || 'unknown'));
  } catch(e) { alert('Undo item error: ' + e.message); }
}

async function manualAssign(files){
  const fd=new FormData(); for(const f of files) fd.append('diagrams', f, f.name);
  const res=await fetch(`/api/diagram_watch/manual_assign/${activeJobId}`,{method:'POST', body:fd});
  const data=await res.json();
  if(data.ok){
    const st=await fetch(`/api/status/${activeJobId}`).then(r=>r.json());
    expectedDiagrams=st.expected_diagrams||expectedDiagrams;
    renderDiagramFinder(activeJobId, expectedDiagrams);
    renderJobPanel(activeJobId, st.status, expectedDiagrams, st);
  }
}

async function runReword(jobId){
  const res=await fetch(`/api/reword/${jobId}`,{method:'POST'});
  const data=await res.json();
  if(data.error) throw new Error(data.error);
  renderJobPanel(jobId,'rewording', expectedDiagrams, {progress:{stage:'rewording', pct:10, msg:'Rewording & running full pipeline...'}});
  startJobPolling();
  return data;
}

async function runRecheck(jobId){
  const res=await fetch(`/api/recheck/${jobId}`,{method:'POST'});
  const data=await res.json();
  if(data.error) throw new Error(data.error);
  renderJobPanel(jobId,'rechecking', expectedDiagrams, {progress:{stage:'rechecking', pct:10, msg:'Rechecking flags...'}});
  startJobPolling();
  return data;
}

function initActions() {
  document.getElementById('btnRefresh').addEventListener('click', () => {
    fetchQuestions(); fetchJobs(); fetchTelemetry();
    if(activeJobId) fetch(`/api/status/${activeJobId}`).then(r=>r.json()).then(j=>{ if(!j.error) { renderJobPanel(activeJobId,j.status,j.expected_diagrams, j); renderJobStepper(activeJobId,j.status,j.expected_diagrams);} });
  });
  document.getElementById('btnExport').addEventListener('click', () => { window.location.href = '/api/export'; });
  const btnExportFinal = document.getElementById('btnExportFinal');
  if (btnExportFinal) btnExportFinal.addEventListener('click', () => { if (activeJobId) window.location.href = `/api/export_final/${activeJobId}`; else window.location.href = '/api/export'; });
  const btnClearAll = document.getElementById('btnClearAll');
  if (btnClearAll) btnClearAll.addEventListener('click', async () => {
    if (!confirm('Clear all extracted questions and jobs history?')) return;
    try { await fetch('/api/questions/clear', { method: 'POST' }); await fetch('/api/jobs/clear', { method: 'POST' }); localStorage.removeItem('activeJobId'); activeJobId=null; document.getElementById('jobPanel').style.display='none'; fetchQuestions(); fetchJobs(); fetchTelemetry(); } catch (e) { alert('Failed to clear: ' + e.message); }
  });
  const btnClearJobs = document.getElementById('btnClearJobs');
  if (btnClearJobs) btnClearJobs.addEventListener('click', async () => { try { await fetch('/api/jobs/clear', { method: 'POST' }); fetchJobs(); } catch (e) { alert('Failed to clear jobs: ' + e.message); } });
  const btnRecheck = document.getElementById('btnRecheck');
  if (btnRecheck) btnRecheck.addEventListener('click', async () => {
    if(!activeJobId) return alert('No active job');
    btnRecheck.disabled = true; btnRecheck.textContent = '⏳ Processing...';
    try { await runRecheck(activeJobId); } catch(err){ alert(err.message);} finally { btnRecheck.disabled = false; btnRecheck.textContent = '⚡ Run AI Recheck'; }
  });
  const btnPick=document.getElementById('btnPickDiagrams');
  const diagInput=document.getElementById('diagramFileInput');
  if(btnPick&&diagInput){
    btnPick.addEventListener('click',()=>diagInput.click());
    diagInput.addEventListener('change', async()=>{ if(!activeJobId) return alert('No active job'); if(diagInput.files.length) await manualAssign(diagInput.files); diagInput.value=''; });
  }
  const btnToggle=document.getElementById('btnToggleWatch');
  if(btnToggle) btnToggle.addEventListener('click', toggleWatch);
  const btnUndo=document.getElementById('btnUndoCapture');
  if(btnUndo) btnUndo.addEventListener('click', undoLast);
  async function discardFile(name){
    await fetch(`/api/diagram_watch/discard/${activeJobId}`,{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({src:name})});
    const picker=document.getElementById('pendingPicker'); if(picker) picker.style.display='none'; await fetchWatchStatus();
  }
  function showPendingPicker(pending){
    const picker=document.getElementById('pendingPicker'); if(!picker) return;
    picker.innerHTML='<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.4rem;"><strong style="font-size:0.8rem; color:#e2e8f0;">Unassigned screenshots — click to discard</strong><button class="btn btn-secondary" style="font-size:0.7rem; padding:0.15rem 0.4rem;" onclick="document.getElementById(\'pendingPicker\').style.display=\'none\'">✕</button></div>'+pending.map(p=>`<button data-name="${escapeHtml(p.name)}" style="display:flex; justify-content:space-between; width:100%; text-align:left; background:#1e293b; border:1px solid #334155; color:#cbd5e1; padding:0.35rem 0.5rem; border-radius:4px; margin-bottom:0.3rem; cursor:pointer; font-size:0.78rem;"><span>${escapeHtml(p.name)}</span><span style="color:#94a3b8;">${(p.size/1024).toFixed(0)} KB</span></button>`).join('');
    picker.style.display='block'; picker.querySelectorAll('button[data-name]').forEach(b=>b.addEventListener('click',()=>discardFile(b.dataset.name)));
  }
  const btnDiscard=document.getElementById('btnDiscardCapture');
  if(btnDiscard) btnDiscard.addEventListener('click', async()=>{
    if(!activeJobId) return;
    try{
      const r=await fetch(`/api/diagram_watch/pending/${activeJobId}`);
      const d=await r.json(); const pending=d.pending||[];
      if(!pending.length){ alert('No unassigned screenshots in watch folder.'); return; }
      if(pending.length===1){ await discardFile(pending[0].name); return; }
      showPendingPicker(pending);
    }catch(e){ alert('Discard failed: '+e.message); }
  });
  const btnProfiles = document.getElementById('btnProfiles');
  const profilesModal = document.getElementById('profilesModal');
  const btnCloseProfiles = document.getElementById('btnCloseProfilesModal');
  const btnProfilesCloseX = document.getElementById('btnProfilesModalClose');
  const btnSyncAll = document.getElementById('btnSyncAllAccounts');
  const btnLoginNew = document.getElementById('btnLoginNewProfile');
  if (btnProfiles) btnProfiles.addEventListener('click', () => { profilesModal.style.display = 'flex'; fetchProfiles(); });
  if (btnCloseProfiles) btnCloseProfiles.addEventListener('click', () => profilesModal.style.display = 'none');
  if (btnProfilesCloseX) btnProfilesCloseX.addEventListener('click', () => profilesModal.style.display = 'none');
  const selectAll = document.getElementById('selectAllProfiles');
  if (selectAll) selectAll.addEventListener('change', () => { document.querySelectorAll('.profile-checkbox:not(:disabled)').forEach(cb => cb.checked = selectAll.checked); });
  if (btnSyncAll) {
    btnSyncAll.addEventListener('click', async () => {
      const selected = getSelectedProfileNames(); const label = selected.length ? `${selected.length} selected` : 'all';
      const timeoutInput = document.getElementById('syncTimeoutInput');
      const timeoutSec = timeoutInput ? (parseInt(timeoutInput.value, 10) || 5) : 5;
      btnSyncAll.disabled = true; btnSyncAll.textContent = `⏳ Starting sync for ${label}...`;
      try {
        const body = selected.length ? { profiles: selected, timeout_seconds: timeoutSec } : { timeout_seconds: timeoutSec };
        const res = await fetch('/api/profiles/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const data = await res.json(); if (!data.ok) throw new Error(data.error || 'Could not start sync');
        const pollInterval = setInterval(async () => {
          try {
            const statusRes = await fetch('/api/profiles/sync/status'); const statusData = await statusRes.json();
            if (statusData.running) { btnSyncAll.textContent = `⏳ Syncing ${statusData.completed}/${statusData.total}...`; fetchProfiles(); }
            else { clearInterval(pollInterval); btnSyncAll.disabled = false; btnSyncAll.textContent = '🔄 Sync All'; fetchProfiles(); alert(`Sync finished! Processed ${statusData.completed} accounts.`); }
          } catch (e) { console.error('Polling error:', e); }
        }, 1500);
      } catch (err) { alert('Sync error: ' + err.message); btnSyncAll.disabled = false; btnSyncAll.textContent = '🔄 Sync All'; }
    });
  }
  const btnLoginSelected = document.getElementById('btnLoginSelected');
  if (btnLoginSelected) {
    btnLoginSelected.addEventListener('click', async () => {
      const selected = getSelectedProfileNames(); if (!selected.length) { alert('Check at least one account to login.'); return; }
      const timeoutInput = document.getElementById('syncTimeoutInput');
      const timeoutSec = timeoutInput ? (parseInt(timeoutInput.value, 10) || 5) : 5;
      btnLoginSelected.disabled = true; btnLoginSelected.textContent = `⏳ Logging in ${selected.length}...`;
      try {
        const body = { profiles: selected, timeout_seconds: timeoutSec };
        const res = await fetch('/api/profiles/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const data = await res.json(); if (!data.ok) throw new Error(data.error || 'Could not start login');
        const pollInterval = setInterval(async () => {
          try { const statusRes = await fetch('/api/profiles/sync/status'); const statusData = await statusRes.json();
            if (statusData.running) btnLoginSelected.textContent = `⏳ ${statusData.completed}/${statusData.total} done...`;
            else { clearInterval(pollInterval); btnLoginSelected.disabled = false; btnLoginSelected.textContent = '🔑 Login Selected'; fetchProfiles(); alert(`Login done! ${statusData.completed} account(s) processed.`); }
          } catch (e) { console.error('Polling error:', e); }
        }, 1500);
      } catch (err) { alert('Login error: ' + err.message); btnLoginSelected.disabled = false; btnLoginSelected.textContent = '🔑 Login Selected'; }
    });
  }
  const btnClean = document.getElementById('btnCleanNotebooks');
  if (btnClean) btnClean.addEventListener('click', async () => {
    btnClean.disabled = true; btnClean.textContent = '🧹 Cleaning...';
    try { const res = await fetch('/api/notebooks/clean', { method: 'POST' }); const data = await res.json(); if (data.ok) alert('Disposable notebooks deleted!'); else alert('Clean failed: ' + (data.error || 'Unknown')); } catch (err) { alert('Clean error: ' + err.message); } finally { btnClean.disabled = false; btnClean.textContent = '🧹 Clean Disposable Notebooks'; }
  });
  if (btnLoginNew) btnLoginNew.addEventListener('click', async () => {
    const input = document.getElementById('newProfileNameInput'); const name = (input.value || '').trim();
    if (!name) { alert('Please enter a profile name (e.g. slave4)'); return; }
    btnLoginNew.disabled = true; btnLoginNew.textContent = '⏳ Opening Chrome...';
    try {
      const res = await fetch(`/api/profiles/${encodeURIComponent(name)}/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
      const data = await res.json(); if (data.ok) { alert(`Account '${name}' logged in as ${data.email || 'Google User'}!`); input.value = ''; fetchProfiles(); populateProviderSelect(); } else alert('Login failed: ' + (data.message || 'Unknown'));
    } catch (err) { alert('Login error: ' + err.message); } finally { btnLoginNew.disabled = false; btnLoginNew.textContent = '➕ Add Account'; }
  });
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active'); 
      activeFilter = e.target.dataset.filter; 
      currentPage = 1;
      renderQuestions();
    });
  });
  document.getElementById('btnModalClose').addEventListener('click', closeModal);
  document.getElementById('btnCancelEdit').addEventListener('click', closeModal);
  document.getElementById('btnSaveEdit').addEventListener('click', saveQuestionEdit);
  document.getElementById('editStemInput').addEventListener('input', (e) => { renderKaTeXIn(document.getElementById('editStemPreview'), e.target.value); });
}

function initTelemetry() { fetchTelemetry(); setInterval(fetchTelemetry, 1200); }
async function fetchTelemetry() {
  try { const res = await fetch('/api/telemetry'); const data = await res.json(); updateTelemetryUI(data); } catch (err) { console.error('Telemetry fetch error:', err); }
}

function updateTelemetryUI(t) {
  const accountsEl = document.getElementById('jobPanelAccounts');
  if (accountsEl && t.accounts && t.accounts.length) {
    accountsEl.innerHTML = t.accounts.map(acc => {
      const state = acc.state || 'idle';
      const countInfo = acc.total_batches ? ` [${acc.total_batches}b]` : '';
      const task = acc.current_task ? ` (${escapeHtml(acc.current_task)})` : '';
      return `<div class="account-chip state-${state}" title="${escapeHtml(acc.email || acc.name)}: ${state} (batches: ${acc.total_batches||0})"><span class="chip-dot"></span><span>${escapeHtml(acc.name)}${countInfo}${task}</span></div>`;
    }).join('');
  }
  const ticker = document.getElementById('jobPanelTicker');
  if (ticker && t.events && t.events.length) {
    const last = t.events[t.events.length - 1];
    ticker.textContent = `[${last.time}] ${last.msg}`;
  }
}

async function fetchProfiles() {
  const container = document.getElementById('profilesListContainer');
  try {
    const res = await fetch('/api/profiles'); const data = await res.json();
    if (!data.profiles || !data.profiles.length) { container.innerHTML = '<p class="empty-state">No Google profiles configured yet.</p>'; return; }
    container.innerHTML = data.profiles.map(p => {
      const isAuth = p.authenticated; const hasStorage = p.has_storage; const rl = p.rate_limited; const isDisabled = p.disabled;
      let statusHtml;
      if (isDisabled) statusHtml = '<span class="status-tag" style="background:rgba(107,114,128,0.15);color:#6b7280;">⏸ Disabled</span>';
      else if (isAuth) statusHtml = '<span class="status-tag status-valid">✅ Session Active</span>';
      else if (hasStorage) statusHtml = '<span class="status-tag status-warning" style="background:rgba(245,158,11,0.15);color:#f59e0b;">⚠️ No Email / Needs Login</span>';
      else statusHtml = '<span class="status-tag status-review">❌ No Session</span>';
      return `<div style="display:flex; align-items:center; gap:0.6rem; background:${isDisabled ? '#111318' : '#151a26'}; padding:0.55rem 0.75rem; border-radius:6px; border:1px solid ${isDisabled ? '#1f2430' : '#262e42'}; opacity:${isDisabled ? '0.6' : '1'};">
        <input type="checkbox" class="profile-checkbox" data-name="${escapeHtml(p.name)}" style="width:15px;height:15px;accent-color:#6366f1;flex-shrink:0;cursor:pointer;" ${isDisabled ? 'disabled' : ''}>
        <div style="flex:1;min-width:0;"><strong style="font-size:0.9rem;">${escapeHtml(p.name)}</strong><span style="font-size:0.78rem; color:${p.email ? '#9ca3af' : '#6b7280'}; margin-left:0.4rem;">${p.email ? escapeHtml(p.email) : '— no email —'}</span>${rl && !isDisabled ? `<span style="font-size:0.72rem;color:#f59e0b;margin-left:0.4rem;">⏳ Rate-limited (${escapeHtml(rl.remaining_human)})</span>` : ''}</div>
        <div style="display:flex;gap:0.4rem;align-items:center;flex-shrink:0;">${statusHtml}
          ${rl && !isDisabled ? `<button class="btn" style="font-size:0.72rem;padding:0.2rem 0.45rem;background:rgba(245,158,11,0.18);color:#fbbf24;border:1px solid #f59e0b44;" onclick="clearProfileRateLimit('${escapeHtml(p.name)}')">⚡ Clear Limit</button>` : ''}
          <button class="btn btn-secondary" style="font-size:0.72rem;padding:0.2rem 0.45rem;" onclick="loginSpecificProfile('${escapeHtml(p.name)}')" ${isDisabled ? 'disabled' : ''}>🔄</button>
          <button class="btn" style="font-size:0.72rem;padding:0.2rem 0.45rem;background:${isDisabled ? 'rgba(99,102,241,0.15)' : 'rgba(239,68,68,0.12)'};color:${isDisabled ? '#818cf8' : '#f87171'};border:1px solid ${isDisabled ? '#4338ca33' : '#ef444433'};" onclick="toggleProfileDisabled('${escapeHtml(p.name)}', ${isDisabled})">${isDisabled ? '▶ Enable' : '⏸ Disable'}</button>
        </div></div>`;
    }).join('');
  } catch (err) { container.innerHTML = `<p class="empty-state" style="color:#ef4444;">Failed to load profiles: ${err.message}</p>`; }
}

async function clearProfileRateLimit(name) {
  try {
    const res = await fetch(`/api/nlm/profiles/${encodeURIComponent(name)}/clear-cooldown`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      fetchProfiles();
      populateProviderSelect();
    } else {
      alert('Failed to clear rate limit: ' + (data.error || 'Unknown'));
    }
  } catch (err) {
    alert('Error: ' + err.message);
  }
}

async function toggleProfileDisabled(name, currentlyDisabled) {
  try { const res = await fetch(`/api/profiles/${encodeURIComponent(name)}/toggle`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ disabled: !currentlyDisabled }) }); const data = await res.json(); if (data.ok) { fetchProfiles(); populateProviderSelect(); } else alert('Toggle failed: ' + (data.message || 'Unknown')); } catch (err) { alert('Toggle error: ' + err.message); }
}

function getSelectedProfileNames() { return Array.from(document.querySelectorAll('.profile-checkbox:checked')).map(cb => cb.dataset.name); }

async function loginSpecificProfile(name) {
  try { const res = await fetch(`/api/profiles/${encodeURIComponent(name)}/login`, { method: 'POST' }); const data = await res.json(); if (data.ok) { alert(`Account '${name}' refreshed!`); fetchProfiles(); populateProviderSelect(); } else alert('Refresh failed: ' + (data.message || 'Unknown')); } catch (err) { alert('Refresh error: ' + err.message); }
}

async function fetchQuestions() {
  try { const res = await fetch('/api/questions'); const data = await res.json(); allQuestions = data.questions || []; document.getElementById('questionCountBadge').textContent = `${allQuestions.length} Questions`; renderQuestions(); } catch (err) { console.error('Failed to load questions:', err); }
}

async function fetchJobs() {
  try {
    const res = await fetch('/api/jobs'); const data = await res.json();
    const jobsList = document.getElementById('jobsList');
    if (!data.jobs || !data.jobs.length) { jobsList.innerHTML = '<p class="empty-state">No jobs executed yet.</p>'; return; }
    jobsList.innerHTML = data.jobs.map(j => {
      const statusColor = j.status === 'completed' ? '#10b981' : j.status === 'failed' ? '#ef4444' : ['extracting','rewording','solving','validating','fixing','rechecking'].includes(j.status) ? '#6366f1' : '#f59e0b';
      const isActive=j.id===activeJobId;
      const rawPct=j.progress?.pct ?? '';
      const pct = rawPct !== '' ? Math.max(0, Math.min(100, Math.round(rawPct))) : '';
      return `<div class="job-row ${isActive?'active':''}" data-job="${escapeHtml(j.id)}" onclick="selectJob('${escapeHtml(j.id)}')" style="font-size:0.8rem; border-bottom:1px solid #262e42;">
        <strong>${escapeHtml(j.filename)}</strong> <span style="color:${statusColor}">● ${escapeHtml(j.status||'unknown')}</span>
        ${pct!==''?`<span style="color:#6366f1"> ${pct}%</span>`:''}
        ${j.total_extracted ? `(${j.total_extracted} extracted)` : ''}
      </div>`;
    }).join('');
  } catch (err) { console.error('Failed to load jobs:', err); }
}

async function selectJob(jobId){
  setActiveJob(jobId);
  const r=await fetch(`/api/status/${jobId}`); const j=await r.json();
  if(j.error) return alert(j.error);
  expectedDiagrams=j.expected_diagrams||[];
  renderJobPanel(jobId, j.status, expectedDiagrams, j);
  if(expectedDiagrams.length || j.status==='awaiting_screenshots') renderDiagramFinder(jobId, expectedDiagrams);
  renderJobStepper(jobId, j.status, expectedDiagrams);
  fetchQuestions();
  startJobPolling();
  if(watchPoll) clearInterval(watchPoll);
  watchPoll=setInterval(fetchWatchStatus,1200);
  fetchWatchStatus();
  document.getElementById('jobPanel')?.scrollIntoView({behavior:'smooth'});
}

let currentPage = 1;
let pageSize = 10;

function changePage(newPage) {
  currentPage = newPage;
  renderQuestions();
  const el = document.getElementById('paginationTop') || document.getElementById('questionsContainer');
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function changePageSize(newSize) {
  if (newSize === 'all') {
    pageSize = 999999;
  } else {
    pageSize = parseInt(newSize, 10) || 10;
  }
  currentPage = 1;
  renderQuestions();
}

function buildPaginationHtml(totalCount, startIdx, endIdx, totalPages) {
  if (totalCount <= 0) return '';

  let btns = `
    <button class="page-btn" ${currentPage <= 1 ? 'disabled' : ''} onclick="changePage(${currentPage - 1})">
      ◀ Prev
    </button>
  `;

  // Always show page 1
  btns += `<button class="page-btn ${currentPage === 1 ? 'active' : ''}" onclick="changePage(1)">1</button>`;

  // Left ellipsis
  if (currentPage > 3) {
    btns += `<span class="page-ellipsis">…</span>`;
  }

  // Window around current page
  const winStart = Math.max(2, currentPage - 1);
  const winEnd = Math.min(totalPages - 1, currentPage + 1);
  for (let p = winStart; p <= winEnd; p++) {
    btns += `<button class="page-btn ${currentPage === p ? 'active' : ''}" onclick="changePage(${p})">${p}</button>`;
  }

  // Right ellipsis
  if (currentPage < totalPages - 2) {
    btns += `<span class="page-ellipsis">…</span>`;
  }

  // Last page button if more than 1 page
  if (totalPages > 1) {
    btns += `<button class="page-btn ${currentPage === totalPages ? 'active' : ''}" onclick="changePage(${totalPages})">${totalPages}</button>`;
  }

  // Next button
  btns += `
    <button class="page-btn" ${currentPage >= totalPages ? 'disabled' : ''} onclick="changePage(${currentPage + 1})">
      Next ▶
    </button>
  `;

  const sizeOptions = [10, 20, 50].map(s => 
    `<option value="${s}" ${pageSize === s ? 'selected' : ''}>${s} per page</option>`
  ).join('') + `<option value="all" ${pageSize > 100 ? 'selected' : ''}>All</option>`;

  return `
    <div class="page-info">
      Showing <strong>${startIdx + 1}–${endIdx}</strong> of <strong>${totalCount}</strong> questions
    </div>
    <div class="pagination-controls">
      ${btns}
    </div>
    <div class="page-size-selector">
      <span>Per page:</span>
      <select class="page-size-select" onchange="changePageSize(this.value)">
        ${sizeOptions}
      </select>
    </div>
  `;
}

function renderQuestions() {
  const container = document.getElementById('questionsContainer');
  const pagTop = document.getElementById('paginationTop');
  const pagBottom = document.getElementById('paginationBottom');

  let filtered = allQuestions;
  if (activeFilter === 'needs_review') filtered = allQuestions.filter(q => q.needs_human_review);
  else if (activeFilter === 'human_review') filtered = allQuestions.filter(q => q.final_status === 'needs_human' || q.needs_human_review);
  else if (activeFilter === 'valid') filtered = allQuestions.filter(q => q.validation_status === 'valid' || ['auto_valid','auto_fixed','human_edited','human_approved'].includes(q.final_status));
  else if (activeFilter === 'recheck_passed') filtered = allQuestions.filter(q => ['auto_valid','done','human_approved'].includes(q.final_status));

  if (!filtered.length) {
    if (pagTop) pagTop.style.display = 'none';
    if (pagBottom) pagBottom.style.display = 'none';
    container.innerHTML = `<div class="empty-state-large"><span class="empty-icon">🔍</span><h3>No questions match filter</h3></div>`;
    return;
  }

  const totalCount = filtered.length;
  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  if (currentPage > totalPages) currentPage = totalPages;
  if (currentPage < 1) currentPage = 1;

  const startIdx = (currentPage - 1) * pageSize;
  const endIdx = Math.min(startIdx + pageSize, totalCount);
  const pageQuestions = filtered.slice(startIdx, endIdx);

  // Render pagination controls
  const pagHtml = buildPaginationHtml(totalCount, startIdx, endIdx, totalPages);
  if (pagTop) {
    pagTop.innerHTML = pagHtml;
    pagTop.style.display = 'flex';
  }
  if (pagBottom) {
    pagBottom.innerHTML = pagHtml;
    pagBottom.style.display = 'flex';
  }

  container.innerHTML = pageQuestions.map((q, idx) => {
    const isValid = q.validation_status === 'valid';
    const fs = q.final_status || (q.needs_human_review ? 'needs_human' : (isValid ? 'auto_valid' : 'needs_human'));
    const ansStatus = q.answer_status || 'unsolved';
    const ansTag = ansStatus==='verified' ? '<span class="status-tag status-valid">✅ Answer Verified</span>' : ansStatus==='flagged' ? '<span class="status-tag status-warning">⚠️ Answer Flagged</span>' : ansStatus==='human_review' ? '<span class="status-tag" style="background:rgba(239,68,68,0.15);color:#f87171">🧑 Answer Needs Review</span>' : '';
    const diagTag = q.diagram_status === 'regenerated' ? `<span class="status-tag status-valid">🎨 Diagram: ${(q.diagram_format || 'code').toUpperCase()}</span>` : q.diagram_status === 'human_review' ? '<span class="status-tag status-warning">⚠️ Diagram Needs Review</span>' : q.diagram_status === 'original' ? '<span class="status-tag" style="background:#1e293b;color:#93c5fd;border:1px solid #3b82f6;">🖼️ Original Diagram</span>' : '';
    const fsTag = (fs === 'needs_human' || q.needs_human_review) ? '<span class="status-tag" style="background:rgba(239,68,68,0.15);color:#f87171;border:1px solid #ef4444;">🧑 Human Review Required</span>' : (fs === 'human_edited' || fs === 'human_approved') ? '<span class="status-tag" style="background:rgba(16,185,129,0.15);color:#6ee7b7;border:1px solid #10b981;">✅ Human Approved</span>' : fs === 'auto_valid' || fs === 'done' ? '<span class="status-tag status-valid">✅ Verified Complete</span>' : '';

    const qType = q.question_type || (q.choices && q.choices.length > 0 ? 'Multiple Choice' : 'Structured');
    const typeTag = `<span class="status-tag" style="background:#1e293b; color:#cbd5e1; border:1px solid #475569;">📋 ${escapeHtml(qType)}</span>`;

    // Calculate Human Review Reasons
    let hrReasons = [];
    if (q.needs_human_review || fs === 'needs_human') {
      if (q.diagram_status === 'human_review' || q.diagram_validation_status === 'fail') {
        const dv = q.diagram_validation || {};
        let diagDetails = dv.fix_instructions || (dv.distorted_elements?.length ? dv.distorted_elements.join('; ') : '') || 'Diagram validation flagged discrepancies with original screenshot';
        hrReasons.push(`<strong>Diagram:</strong> ${escapeHtml(diagDetails)}`);
      }
      if (ansStatus === 'flagged' || ansStatus === 'human_review' || (q.answer_validation && q.answer_validation.matches === false)) {
        let ansDetails = q.answer_validation?.mismatch_note || q.possible_error_note || 'Automated answer verification divergence';
        hrReasons.push(`<strong>Answer:</strong> ${escapeHtml(ansDetails)}`);
      }
      if (!isValid && q.validation_errors?.length) {
        hrReasons.push(`<strong>KaTeX / Notation:</strong> ${escapeHtml(q.validation_errors.join(', '))}`);
      }
      if (!hrReasons.length && (!q.choices || q.choices.length === 0)) {
        hrReasons.push(`<strong>Open-Ended Structured Question:</strong> Mathematical derivation requires human sign-off.`);
      }
    }

    const hrReasonHtml = hrReasons.length ? `
      <div style="background:rgba(239,68,68,0.08); border-left:4px solid #ef4444; border-radius:4px; padding:0.6rem 0.8rem; margin:0.6rem 0 0.8rem 0; font-size:0.83rem; color:#fca5a5;">
        <div style="font-weight:700; color:#f87171; margin-bottom:0.25rem;">⚠️ Why Human Review Is Required:</div>
        <div>${hrReasons.join('<br>')}</div>
      </div>
    ` : '';

    // Diagram card rendering — side-by-side comparison when both exist
    let diagramHtml = '';
    if (q.diagram_file) {
      const hasOriginal = Boolean(q.diagram_original_file);
      const hasRegen = Boolean(q.diagram_rendered_file);
      const activeSource = q.diagram_active_source || (
        q.diagram_file === q.diagram_rendered_file ? 'regenerated' :
        q.diagram_file === q.diagram_original_file ? 'original' :
        q.diagram_file.includes('_regen') ? 'regenerated' : 'original'
      );

      if (hasOriginal && hasRegen) {
        // Side-by-side comparison mode
        const origActive = activeSource === 'original';
        diagramHtml = `
          <div class="diagram-preview-card" style="margin:0.8rem 0; background:#0f172a; border:1px solid #334155; border-radius:8px; padding:0.75rem;">
            <div style="font-size:0.78rem; color:#94a3b8; margin-bottom:0.6rem; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:0.4rem;">
              <span><strong style="color:#e2e8f0;">📊 Diagram Comparison</strong> — Click <em>Use This</em> to select which version to export</span>
              <button class="btn btn-secondary" style="font-size:0.75rem; padding:0.2rem 0.5rem; background:#312e81; color:#c7d2fe; border-color:#4338ca;" onclick="rerunDiagramFix('${q.id}', this)">🔄 Rerun Fix</button>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.75rem;">
              <div style="border:2px solid ${origActive ? '#3b82f6' : '#1e293b'}; border-radius:8px; padding:0.6rem; background:${origActive ? 'rgba(59,130,246,0.07)' : '#0a0f1a'}; transition:all 0.2s;">
                <div style="font-size:0.72rem; font-weight:700; color:${origActive ? '#60a5fa' : '#64748b'}; margin-bottom:0.4rem; display:flex; align-items:center; justify-content:space-between;">
                  <span>🖼️ Original Screenshot ${origActive ? '— <span style="color:#22d3ee">Active</span>' : ''}</span>
                </div>
                <img src="/diagrams/${q.diagram_original_file}" alt="Original Diagram"
                  style="width:100%; max-height:230px; object-fit:contain; border-radius:5px; display:block; cursor:pointer;"
                  onerror="this.style.display='none'"
                  onclick="window.open('/diagrams/${q.diagram_original_file}','_blank')">
                <button class="btn btn-primary" style="width:100%; margin-top:0.5rem; font-size:0.75rem; padding:0.25rem; ${origActive ? 'opacity:0.45; cursor:default;' : 'background:#1e3a5f; border-color:#3b82f6; color:#93c5fd;'}"
                  onclick="selectDiagramSource('${q.id}','original')" ${origActive ? 'disabled' : ''}>
                  ${origActive ? '✅ Currently Active' : '🖼️ Use This (Original)'}
                </button>
              </div>
              <div style="border:2px solid ${!origActive ? '#10b981' : '#1e293b'}; border-radius:8px; padding:0.6rem; background:${!origActive ? 'rgba(16,185,129,0.07)' : '#0a0f1a'}; transition:all 0.2s;">
                <div style="font-size:0.72rem; font-weight:700; color:${!origActive ? '#34d399' : '#64748b'}; margin-bottom:0.4rem; display:flex; align-items:center; justify-content:space-between;">
                  <span>🎨 Clean Regenerated (${(q.diagram_format||'Code').toUpperCase()}) ${!origActive ? '— <span style="color:#22d3ee">Active</span>' : ''}</span>
                </div>
                <img src="/diagrams/${q.diagram_rendered_file}" alt="Regenerated Diagram"
                  style="width:100%; max-height:230px; object-fit:contain; border-radius:5px; display:block; cursor:pointer;"
                  onerror="this.style.display='none'"
                  onclick="window.open('/diagrams/${q.diagram_rendered_file}','_blank')">
                <button class="btn btn-primary" style="width:100%; margin-top:0.5rem; font-size:0.75rem; padding:0.25rem; ${!origActive ? 'opacity:0.45; cursor:default;' : 'background:#064e3b; border-color:#10b981; color:#6ee7b7;'}"
                  onclick="selectDiagramSource('${q.id}','regenerated')" ${!origActive ? 'disabled' : ''}>
                  ${!origActive ? '✅ Currently Active' : '🎨 Use This (Clean)'}
                </button>
              </div>
            </div>
          </div>
        `;
      } else {
        // Single image fallback card
        const isRegen = hasRegen && !hasOriginal;
        diagramHtml = `
          <div class="diagram-preview-card" style="margin:0.8rem 0; background:#0f172a; border:1px solid #334155; border-radius:8px; padding:0.75rem;">
            <div style="text-align:center;">
              <img src="/diagrams/${q.diagram_file}" alt="Diagram" class="diagram-img" style="max-height:280px; max-width:100%; border-radius:6px; object-fit:contain;" onerror="this.style.display='none'">
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem; margin-top:0.6rem; border-top:1px solid #1e293b; padding-top:0.5rem;">
              <div style="font-size:0.78rem; color:#94a3b8;">
                <span style="font-weight:600; color:#e2e8f0;">Active Diagram:</span>
                ${isRegen ? `<span style="color:#10b981; font-weight:700;">Clean Vector (${(q.diagram_format||'Code').toUpperCase()})</span>` : '<span style="color:#60a5fa; font-weight:700;">Original Screenshot</span>'}
              </div>
              <button class="btn btn-secondary" style="font-size:0.75rem; padding:0.25rem 0.55rem; background:#312e81; color:#c7d2fe; border-color:#4338ca;" onclick="rerunDiagramFix('${q.id}', this)">
                🔄 Rerun Diagram Fix
              </button>
            </div>
          </div>
        `;
      }
    }

    const approveBtn = (fs === 'needs_human' || q.needs_human_review) ? `
      <button class="btn btn-primary" style="font-size:0.8rem; padding:0.3rem 0.75rem; background:#059669; border-color:#10b981;" onclick="approveQuestion('${q.id}')">
        ✅ Approve Question
      </button>
    ` : '';

    return `
      <div class="question-card" id="qcard-${q.id}">
        <div class="card-top">
          <span style="font-size:0.85rem; font-weight:600; color:#9ca3af;">
            #${q.question_number || (startIdx + idx + 1)} · ${q.source_file || 'PDF'} · Path: ${q.label_path || q.question_number}
          </span>
          <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap;">
            ${typeTag}${fsTag}${ansTag}${diagTag}
            <span class="status-tag ${isValid ? 'status-valid' : 'status-review'}">${isValid ? '✅ Valid KaTeX' : '⚠️ ' + (q.validation_errors?.[0] || 'Needs Review')}</span>
          </div>
        </div>
        ${hrReasonHtml}
        ${q.context_latex ? `<div class="context-stimulus math-content"><em>${escapeHtml(q.context_latex)}</em></div>` : ''}
        ${q.table ? renderTableHtml(q.table) : ""}
        <div class="stem-text math-content">${escapeHtml(q.reworded_stem || q.raw_stem)}</div>
        ${diagramHtml}
        <div class="choices-list">
          ${(q.choices || []).map((c, i) => `<div class="choice-box ${q.correct_choice_index === i ? 'correct' : ''} math-content"><strong>(${String.fromCharCode(65 + i)})</strong> ${escapeHtml(c)}</div>`).join('')}
        </div>
        ${q.explanation_latex ? `<div class="explanation-box math-content"><strong>💡 Answer Key / Solution:</strong> ${escapeHtml(q.explanation_latex)}</div>` : ''}
        ${q.diagram_description ? `<div style="font-size:0.8rem; color:#9ca3af; margin-bottom:0.75rem;"><strong>Diagram Description:</strong> ${escapeHtml(q.diagram_description)}</div>` : ''}
        ${q.extraction_notes ? `<div style="font-size:0.75rem; color:#6b7280; margin-bottom:0.75rem;"><em>${escapeHtml(q.extraction_notes)}</em></div>` : ''}
        <div class="card-actions" style="display:flex; gap:0.5rem; justify-content:flex-end;">
          ${approveBtn}
          <button class="btn btn-secondary" style="font-size:0.8rem; padding:0.3rem 0.6rem;" onclick="openEditModal('${q.id}')">✏️ Edit</button>
        </div>
      </div>
    `;
  }).join('');
  if (window.renderMathInElement) renderMathInElement(container, { delimiters: [{ left: '\\[', right: '\\]', display: true },{ left: '\\(', right: '\\)', display: false }], throwOnError: false });
}

async function approveQuestion(qId) {
  try {
    const res = await fetch(`/api/questions/${qId}/approve`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      const idx = allQuestions.findIndex(x => x.id === qId);
      if (idx !== -1) allQuestions[idx] = data.question;
      renderQuestions();
    } else {
      alert('Approval failed: ' + (data.error || 'Unknown error'));
    }
  } catch (err) {
    alert('Failed to approve question: ' + err.message);
  }
}

async function toggleDiagram(qId) {
  try {
    const res = await fetch(`/api/diagram/toggle/${qId}`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      const q = allQuestions.find(x => x.id === qId);
      if (q) {
        q.diagram_file = data.diagram_file;
        q.diagram_active_source = data.active_source;
        renderQuestions();
      }
    } else {
      alert('Diagram toggle failed: ' + (data.error || 'Unknown error'));
    }
  } catch (err) {
    alert('Failed to toggle diagram: ' + err.message);
  }
}

async function selectDiagramSource(qId, targetSource) {
  // Find the question so we can check if it's already active
  const q = allQuestions.find(x => x.id === qId);
  if (!q) return;
  const activeSource = q.diagram_active_source || (
    q.diagram_file === q.diagram_rendered_file ? 'regenerated' :
    q.diagram_file === q.diagram_original_file ? 'original' :
    q.diagram_file?.includes('_regen') ? 'regenerated' : 'original'
  );
  if (activeSource === targetSource) return; // already active, nothing to do
  try {
    const res = await fetch(`/api/diagram/select/${qId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: targetSource })
    });
    const data = await res.json();
    if (data.ok) {
      const idx = allQuestions.findIndex(x => x.id === qId);
      if (idx !== -1) {
        allQuestions[idx].diagram_file = data.diagram_file;
        allQuestions[idx].diagram_active_source = data.active_source;
      }
      renderQuestions();
    } else {
      alert('Diagram selection failed: ' + (data.error || 'Unknown error'));
    }
  } catch (err) {
    alert('Failed to select diagram: ' + err.message);
  }
}


async function rerunDiagramFix(qId, btn) {
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '⏳ Fixing diagram...';
  }
  try {
    const res = await fetch(`/api/diagram/fix/${qId}`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      const idx = allQuestions.findIndex(x => x.id === qId);
      if (idx !== -1) allQuestions[idx] = data.question;
      renderQuestions();
    } else {
      alert('Diagram fix failed: ' + (data.error || 'Unknown error'));
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '🔄 Rerun Diagram Fix';
      }
    }
  } catch (err) {
    alert('Failed to rerun diagram fix: ' + err.message);
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '🔄 Rerun Diagram Fix';
    }
  }
}

function renderTableHtml(table){ if(!table||!table.headers) return ''; let h='<div><table><thead><tr>'+table.headers.map(c=>'<th>'+c+'</th>').join('')+'</tr></thead><tbody>'+table.rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>'; return h; }

function openEditModal(qId) {
  const q = allQuestions.find(x => x.id === qId); if (!q) return;
  document.getElementById('editQuestionId').value = q.id;
  document.getElementById('editStemInput').value = q.reworded_stem || q.raw_stem || '';
  document.getElementById('editChoice0').value = q.choices?.[0] || '';
  document.getElementById('editChoice1').value = q.choices?.[1] || '';
  document.getElementById('editChoice2').value = q.choices?.[2] || '';
  document.getElementById('editChoice3').value = q.choices?.[3] || '';
  document.getElementById('editCorrectIndex').value = q.correct_choice_index !== null && q.correct_choice_index !== undefined ? q.correct_choice_index : '';
  document.getElementById('editDiagramDesc').value = q.diagram_description || '';
  renderKaTeXIn(document.getElementById('editStemPreview'), document.getElementById('editStemInput').value);
  document.getElementById('editModal').style.display = 'flex';
}

function closeModal() { document.getElementById('editModal').style.display = 'none'; }

async function saveQuestionEdit() {
  const qId = document.getElementById('editQuestionId').value;
  const q = allQuestions.find(x => x.id === qId); if (!q) return;
  const stem = document.getElementById('editStemInput').value;
  const c0 = document.getElementById('editChoice0').value; const c1 = document.getElementById('editChoice1').value; const c2 = document.getElementById('editChoice2').value; const c3 = document.getElementById('editChoice3').value;
  const corrIdxVal = document.getElementById('editCorrectIndex').value; const diagDesc = document.getElementById('editDiagramDesc').value;
  q.reworded_stem = stem; q.choices = [c0, c1, c2, c3].filter(x => x.trim() !== ''); q.correct_choice_index = corrIdxVal !== '' ? parseInt(corrIdxVal) : null; q.diagram_description = diagDesc || null;
  try {
    const res = await fetch(`/api/questions/${qId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(q) });
    const data = await res.json(); if (data.ok) { closeModal(); fetchQuestions(); } else alert('Save failed: ' + (data.error || 'Unknown'));
  } catch (err) { alert('Failed to save edit: ' + err.message); }
}

function renderKaTeXIn(el, text) {
  el.textContent = text;
  if (window.renderMathInElement) renderMathInElement(el, { delimiters: [{ left: '\\[', right: '\\]', display: true },{ left: '\\(', right: '\\)', display: false }], throwOnError: false });
}

function escapeHtml(str) { if (!str) return ''; return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
