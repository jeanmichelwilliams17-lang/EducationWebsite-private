let allQuestions = [];
let activeFilter = 'all';
let selectedFilename = null;
let selectedAnsFilename = null;
let telemetryPollTimer = null;

document.addEventListener('DOMContentLoaded', () => {
  initUpload();
  initActions();
  fetchQuestions();
  fetchJobs();
  initTelemetry();
  setInterval(fetchJobs, 4000);
});

function initUpload() {
  const uploadZone = document.getElementById('uploadZone');
  const fileInput = document.getElementById('fileInput');
  const fileSelectedName = document.getElementById('fileSelectedName');

  const ansUploadZone = document.getElementById('ansUploadZone');
  const ansFileInput = document.getElementById('ansFileInput');
  const ansFileSelectedName = document.getElementById('ansFileSelectedName');

  const btnStart = document.getElementById('btnStartExtraction');

  // Drag & drop handlers for Question file
  uploadZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadZone.style.borderColor = '#6366f1';
  });
  uploadZone.addEventListener('dragleave', () => {
    uploadZone.style.borderColor = '';
  });
  uploadZone.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadZone.style.borderColor = '';
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      handleFileUpload(e.dataTransfer.files[0], 'question');
    }
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files && fileInput.files.length) {
      handleFileUpload(fileInput.files[0], 'question');
    }
  });

  // Drag & drop handlers for Answer Key file
  if (ansUploadZone) {
    ansUploadZone.addEventListener('dragover', (e) => {
      e.preventDefault();
      ansUploadZone.style.borderColor = '#10b981';
    });
    ansUploadZone.addEventListener('dragleave', () => {
      ansUploadZone.style.borderColor = '';
    });
    ansUploadZone.addEventListener('drop', (e) => {
      e.preventDefault();
      ansUploadZone.style.borderColor = '';
      if (e.dataTransfer.files && e.dataTransfer.files.length) {
        handleFileUpload(e.dataTransfer.files[0], 'answer');
      }
    });

    if (ansFileInput) {
      ansFileInput.addEventListener('change', () => {
        if (ansFileInput.files && ansFileInput.files.length) {
          handleFileUpload(ansFileInput.files[0], 'answer');
        }
      });
    }
  }

  const uploadPrompt = document.getElementById('uploadPrompt');
  const ansUploadPrompt = document.getElementById('ansUploadPrompt');

  async function handleFileUpload(file, type) {
    const formData = new FormData();
    formData.append('file', file);

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
          if (uploadZone) {
            uploadZone.style.borderColor = '#10b981';
            uploadZone.style.backgroundColor = 'rgba(16, 185, 129, 0.08)';
          }
          if (uploadPrompt) {
            uploadPrompt.innerHTML = `
              <span class="upload-icon" style="color:#10b981;">✅</span>
              <p style="color:#10b981; font-weight:600; font-size:0.95rem;">${escapeHtml(data.filename)}</p>
              <span class="file-hint" style="color:#9ca3af;">Click or drag to replace file</span>
            `;
          }
        } else {
          selectedAnsFilename = data.filename;
          targetLabel.textContent = '✅ Answer Key: ' + data.filename;
          if (ansUploadZone) {
            ansUploadZone.style.borderColor = '#10b981';
            ansUploadZone.style.backgroundColor = 'rgba(16, 185, 129, 0.08)';
          }
          if (ansUploadPrompt) {
            ansUploadPrompt.innerHTML = `
              <span class="upload-icon" style="color:#10b981; font-size:1.3rem;">✅</span>
              <p style="color:#10b981; font-weight:600; font-size:0.85rem;">${escapeHtml(data.filename)}</p>
              <span class="file-hint" style="color:#9ca3af;">Click or drag to replace answer key</span>
            `;
          }
        }
      } else {
        targetLabel.textContent = 'Error: ' + (data.error || 'Upload failed');
      }
    } catch (err) {
      targetLabel.textContent = 'Upload failed: ' + err.message;
    }
  }

  btnStart.addEventListener('click', async () => {
    if (!selectedFilename) return;
    const provider = document.getElementById('providerSelect').value;
    btnStart.disabled = true;
    btnStart.textContent = '⏳ Dispatching parallel workers...';

    try {
      const res = await fetch('/api/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: selectedFilename,
          answer_key_filename: selectedAnsFilename,
          provider: provider
        }),
      });
      const data = await res.json();
      if (data.ok) {
        document.getElementById('telemetrySection').style.display = 'block';
        fetchTelemetry();
        fetchJobs();
        setTimeout(fetchQuestions, 1500);
      }
    } catch (err) {
      alert('Failed to start extraction: ' + err.message);
    } finally {
      btnStart.disabled = false;
      btnStart.textContent = '⚡ Start Parallel Extraction & Structuring';
    }
  });
}

function initActions() {
  document.getElementById('btnRefresh').addEventListener('click', () => {
    fetchQuestions();
    fetchJobs();
    fetchTelemetry();
  });

  document.getElementById('btnExport').addEventListener('click', () => {
    window.location.href = '/api/export';
  });

  const btnClearAll = document.getElementById('btnClearAll');
  if (btnClearAll) {
    btnClearAll.addEventListener('click', async () => {
      if (!confirm('Clear all extracted questions and jobs history?')) return;
      try {
        await fetch('/api/questions/clear', { method: 'POST' });
        await fetch('/api/jobs/clear', { method: 'POST' });
        fetchQuestions();
        fetchJobs();
        fetchTelemetry();
      } catch (e) {
        alert('Failed to clear: ' + e.message);
      }
    });
  }

  const btnClearJobs = document.getElementById('btnClearJobs');
  if (btnClearJobs) {
    btnClearJobs.addEventListener('click', async () => {
      try {
        await fetch('/api/jobs/clear', { method: 'POST' });
        fetchJobs();
      } catch (e) {
        alert('Failed to clear jobs: ' + e.message);
      }
    });
  }

  const btnRecheck = document.getElementById('btnRecheck');
  if (btnRecheck) {
    btnRecheck.addEventListener('click', async () => {
      const provider = document.getElementById('providerSelect').value;
      btnRecheck.disabled = true;
      btnRecheck.textContent = '⏳ Verifying solutions...';
      try {
        const res = await fetch('/api/recheck', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider })
        });
        const data = await res.json();
        if (data.ok) {
          alert(`AI Recheck Complete!\nRechecked: ${data.total_rechecked}\nIssues Flagged: ${data.issues_flagged}`);
          fetchQuestions();
        } else {
          alert('Recheck failed: ' + (data.error || 'Unknown error'));
        }
      } catch (err) {
        alert('Recheck error: ' + err.message);
      } finally {
        btnRecheck.disabled = false;
        btnRecheck.textContent = '⚡ Run AI Recheck';
      }
    });
  }

  // Google Accounts Modal Setup
  const btnProfiles = document.getElementById('btnProfiles');
  const profilesModal = document.getElementById('profilesModal');
  const btnCloseProfiles = document.getElementById('btnCloseProfilesModal');
  const btnProfilesCloseX = document.getElementById('btnProfilesModalClose');
  const btnSyncAll = document.getElementById('btnSyncAllAccounts');
  const btnLoginNew = document.getElementById('btnLoginNewProfile');

  if (btnProfiles) {
    btnProfiles.addEventListener('click', () => {
      profilesModal.style.display = 'flex';
      fetchProfiles();
    });
  }

  if (btnCloseProfiles) btnCloseProfiles.addEventListener('click', () => profilesModal.style.display = 'none');
  if (btnProfilesCloseX) btnProfilesCloseX.addEventListener('click', () => profilesModal.style.display = 'none');

  const selectAll = document.getElementById('selectAllProfiles');
  if (selectAll) {
    selectAll.addEventListener('change', () => {
      document.querySelectorAll('.profile-checkbox:not(:disabled)').forEach(cb => cb.checked = selectAll.checked);
    });
  }

  if (btnSyncAll) {
    btnSyncAll.addEventListener('click', async () => {
      const selected = getSelectedProfileNames();
      const label = selected.length ? `${selected.length} selected` : 'all';
      btnSyncAll.disabled = true;
      btnSyncAll.textContent = `⏳ Starting sync for ${label}...`;
      try {
        const body = selected.length ? { profiles: selected } : {};
        const res = await fetch('/api/profiles/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'Could not start sync');

        const pollInterval = setInterval(async () => {
          try {
            const statusRes = await fetch('/api/profiles/sync/status');
            const statusData = await statusRes.json();
            if (statusData.running) {
              btnSyncAll.textContent = `⏳ Syncing ${statusData.completed}/${statusData.total} accounts...`;
              fetchProfiles();
            } else {
              clearInterval(pollInterval);
              btnSyncAll.disabled = false;
              btnSyncAll.textContent = '🔄 Sync All Accounts';
              fetchProfiles();
              alert(`Sync finished! Processed ${statusData.completed} accounts.`);
            }
          } catch (e) { console.error('Polling error:', e); }
        }, 2000);

      } catch (err) {
        alert('Sync error: ' + err.message);
        btnSyncAll.disabled = false;
        btnSyncAll.textContent = '🔄 Sync All Accounts';
      }
    });
  }

  const btnLoginSelected = document.getElementById('btnLoginSelected');
  if (btnLoginSelected) {
    btnLoginSelected.addEventListener('click', async () => {
      const selected = getSelectedProfileNames();
      if (!selected.length) { alert('Check at least one account to login.'); return; }
      btnLoginSelected.disabled = true;
      btnLoginSelected.textContent = `⏳ Logging in ${selected.length} account(s)...`;
      try {
        const body = { profiles: selected };
        const res = await fetch('/api/profiles/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || 'Could not start login');
        const pollInterval = setInterval(async () => {
          try {
            const statusRes = await fetch('/api/profiles/sync/status');
            const statusData = await statusRes.json();
            if (statusData.running) {
              btnLoginSelected.textContent = `⏳ ${statusData.completed}/${statusData.total} done...`;
            } else {
              clearInterval(pollInterval);
              btnLoginSelected.disabled = false;
              btnLoginSelected.textContent = '🔑 Login Selected';
              fetchProfiles();
              alert(`Login done! ${statusData.completed} account(s) processed.`);
            }
          } catch (e) { console.error('Polling error:', e); }
        }, 2000);
      } catch (err) {
        alert('Login error: ' + err.message);
        btnLoginSelected.disabled = false;
        btnLoginSelected.textContent = '🔑 Login Selected';
      }
    });
  }

  const btnClean = document.getElementById('btnCleanNotebooks');
  if (btnClean) {
    btnClean.addEventListener('click', async () => {
      btnClean.disabled = true;
      btnClean.textContent = '🧹 Cleaning Notebooks...';
      try {
        const res = await fetch('/api/notebooks/clean', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          alert('Disposable notebooks deleted across all Google accounts!');
        } else {
          alert('Clean failed: ' + (data.error || 'Unknown error'));
        }
      } catch (err) {
        alert('Clean error: ' + err.message);
      } finally {
        btnClean.disabled = false;
        btnClean.textContent = '🧹 Clean Disposable Notebooks';
      }
    });
  }

  if (btnLoginNew) {
    btnLoginNew.addEventListener('click', async () => {
      const input = document.getElementById('newProfileNameInput');
      const name = (input.value || '').trim();
      if (!name) {
        alert('Please enter a profile name (e.g. slave4)');
        return;
      }
      btnLoginNew.disabled = true;
      btnLoginNew.textContent = '⏳ Opening Chrome...';
      try {
        const res = await fetch(`/api/profiles/${encodeURIComponent(name)}/login`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const data = await res.json();
        if (data.ok) {
          alert(`Account '${name}' logged in successfully as ${data.email || 'Google User'}!`);
          input.value = '';
          fetchProfiles();
        } else {
          alert('Login failed: ' + (data.message || 'Unknown error'));
        }
      } catch (err) {
        alert('Login error: ' + err.message);
      } finally {
        btnLoginNew.disabled = false;
        btnLoginNew.textContent = '➕ Add Account';
      }
    });
  }

  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      activeFilter = e.target.dataset.filter;
      renderQuestions();
    });
  });

  // Modal setup
  document.getElementById('btnModalClose').addEventListener('click', closeModal);
  document.getElementById('btnCancelEdit').addEventListener('click', closeModal);
  document.getElementById('btnSaveEdit').addEventListener('click', saveQuestionEdit);

  document.getElementById('editStemInput').addEventListener('input', (e) => {
    renderKaTeXIn(document.getElementById('editStemPreview'), e.target.value);
  });
}

// ─── Real-Time Telemetry & Progress Polling ───────────────────────────

function initTelemetry() {
  fetchTelemetry();
  setInterval(fetchTelemetry, 1200);
}

async function fetchTelemetry() {
  try {
    const res = await fetch('/api/telemetry');
    const data = await res.json();
    updateTelemetryUI(data);
  } catch (err) {
    console.error('Telemetry fetch error:', err);
  }
}

function updateTelemetryUI(t) {
  const banner = document.getElementById('telemetrySection');
  if (!banner) return;

  const isRunning = t.stage && t.stage !== 'idle' && t.stage !== 'completed' && t.stage !== 'failed';

  if (isRunning || (t.total > 0 && t.completed < t.total)) {
    banner.style.display = 'block';
  } else if (t.stage === 'completed' && t.total > 0) {
    banner.style.display = 'block';
  }

  // Update Progress Bar & Counters
  const pBar = document.getElementById('telemetryProgressBar');
  const pPct = document.getElementById('telemetryPercentage');
  const pText = document.getElementById('telemetryProgressText');
  const pTimer = document.getElementById('telemetryTimer');
  const pJobLabel = document.getElementById('telemetryJobLabel');
  const pStage = document.getElementById('telemetryStageBadge');
  const pEvent = document.getElementById('telemetryLatestEvent');

  if (pBar) pBar.style.width = `${t.percentage || 0}%`;
  if (pPct) pPct.textContent = `${t.percentage || 0}%`;
  if (pText) pText.textContent = `${t.completed || 0} / ${t.total || 0} Questions`;
  if (pTimer) pTimer.textContent = `⏱️ ${t.elapsed_seconds || 0}s`;
  if (pJobLabel) pJobLabel.textContent = t.filename ? `Processing: ${t.filename}` : 'Extraction Pipeline';

  if (pStage) {
    const stageLabels = {
      'ocr_crop': '⚙️ OCR & Smart Crop',
      'parallel_extraction': '⚡ Parallel AI Extraction',
      'critique_refine': '🔄 Critique & Refine',
      'completed': '✅ Extraction Completed',
      'failed': '❌ Extraction Interrupted',
      'idle': '💤 Idle'
    };
    pStage.textContent = stageLabels[t.stage] || t.stage;
  }

  // Update Account Pool Grid
  const grid = document.getElementById('accountStripGrid');
  if (grid && t.accounts && t.accounts.length) {
    grid.innerHTML = t.accounts.map(acc => {
      const state = acc.state || 'idle';
      const task = acc.current_task ? ` (${escapeHtml(acc.current_task)})` : '';
      return `
        <div class="account-chip state-${state}" title="${escapeHtml(acc.email || acc.name)}: ${state}">
          <span class="chip-dot"></span>
          <span>${escapeHtml(acc.name)}${task}</span>
        </div>
      `;
    }).join('');
  }

  // Update Latest Event
  if (pEvent && t.events && t.events.length) {
    const last = t.events[t.events.length - 1];
    pEvent.textContent = `[${last.time}] ${last.msg}`;
  }

  // If questions changed, auto refresh cards
  if (isRunning && t.completed > 0) {
    fetchQuestions();
  }
}

async function fetchProfiles() {
  const container = document.getElementById('profilesListContainer');
  try {
    const res = await fetch('/api/profiles');
    const data = await res.json();
    if (!data.profiles || !data.profiles.length) {
      container.innerHTML = '<p class="empty-state">No Google profiles configured yet. Click "Add Account" above to configure accounts.</p>';
      return;
    }

    container.innerHTML = data.profiles.map(p => {
      const isAuth = p.authenticated;
      const hasStorage = p.has_storage;
      const rl = p.rate_limited;
      const isDisabled = p.disabled;

      let statusHtml;
      if (isDisabled) {
        statusHtml = '<span class="status-tag" style="background:rgba(107,114,128,0.15);color:#6b7280;">⏸ Disabled</span>';
      } else if (isAuth) {
        statusHtml = '<span class="status-tag status-valid">✅ Session Active</span>';
      } else if (hasStorage) {
        statusHtml = '<span class="status-tag status-warning" style="background:rgba(245,158,11,0.15);color:#f59e0b;">⚠️ No Email / Needs Login</span>';
      } else {
        statusHtml = '<span class="status-tag status-review">❌ No Session</span>';
      }

      return `
        <div style="display:flex; align-items:center; gap:0.6rem; background:${isDisabled ? '#111318' : '#151a26'}; padding:0.55rem 0.75rem; border-radius:6px; border:1px solid ${isDisabled ? '#1f2430' : '#262e42'}; opacity:${isDisabled ? '0.6' : '1'};">
          <input type="checkbox" class="profile-checkbox" data-name="${escapeHtml(p.name)}"
            style="width:15px;height:15px;accent-color:#6366f1;flex-shrink:0;cursor:pointer;"
            ${isDisabled ? 'disabled' : ''}>
          <div style="flex:1;min-width:0;">
            <strong style="font-size:0.9rem;">${escapeHtml(p.name)}</strong>
            <span style="font-size:0.78rem; color:${p.email ? '#9ca3af' : '#6b7280'}; margin-left:0.4rem;">
              ${p.email ? escapeHtml(p.email) : '— no email attached —'}
            </span>
            ${rl && !isDisabled ? `<span style="font-size:0.72rem;color:#f59e0b;margin-left:0.4rem;">⏳ Rate-limited (${escapeHtml(rl.remaining_human)})</span>` : ''}
          </div>
          <div style="display:flex;gap:0.4rem;align-items:center;flex-shrink:0;">
            ${statusHtml}
            <button class="btn btn-secondary" style="font-size:0.72rem;padding:0.2rem 0.45rem;"
              onclick="loginSpecificProfile('${escapeHtml(p.name)}')" ${isDisabled ? 'disabled' : ''}>🔄</button>
            <button class="btn" style="font-size:0.72rem;padding:0.2rem 0.45rem;background:${isDisabled ? 'rgba(99,102,241,0.15)' : 'rgba(239,68,68,0.12)'};color:${isDisabled ? '#818cf8' : '#f87171'};border:1px solid ${isDisabled ? '#4338ca33' : '#ef444433'};"
              onclick="toggleProfileDisabled('${escapeHtml(p.name)}', ${isDisabled})">
              ${isDisabled ? '▶ Enable' : '⏸ Disable'}
            </button>
          </div>
        </div>
      `;
    }).join('');
  } catch (err) {
    container.innerHTML = `<p class="empty-state" style="color:#ef4444;">Failed to load profiles: ${err.message}</p>`;
  }
}

async function toggleProfileDisabled(name, currentlyDisabled) {
  try {
    const res = await fetch(`/api/profiles/${encodeURIComponent(name)}/toggle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disabled: !currentlyDisabled })
    });
    const data = await res.json();
    if (data.ok) {
      fetchProfiles();
    } else {
      alert('Toggle failed: ' + (data.message || 'Unknown error'));
    }
  } catch (err) {
    alert('Toggle error: ' + err.message);
  }
}

function getSelectedProfileNames() {
  return Array.from(document.querySelectorAll('.profile-checkbox:checked')).map(cb => cb.dataset.name);
}

async function loginSpecificProfile(name) {
  try {
    const res = await fetch(`/api/profiles/${encodeURIComponent(name)}/login`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      alert(`Account '${name}' refreshed!`);
      fetchProfiles();
    } else {
      alert('Refresh failed: ' + (data.message || 'Unknown error'));
    }
  } catch (err) {
    alert('Refresh error: ' + err.message);
  }
}

async function fetchQuestions() {
  try {
    const res = await fetch('/api/questions');
    const data = await res.json();
    allQuestions = data.questions || [];
    document.getElementById('questionCountBadge').textContent = `${allQuestions.length} Questions`;
    renderQuestions();
  } catch (err) {
    console.error('Failed to load questions:', err);
  }
}

async function fetchJobs() {
  try {
    const res = await fetch('/api/jobs');
    const data = await res.json();
    const jobsList = document.getElementById('jobsList');
    if (!data.jobs || !data.jobs.length) {
      jobsList.innerHTML = '<p class="empty-state">No jobs executed yet.</p>';
      return;
    }

    jobsList.innerHTML = data.jobs.map(j => {
      const statusColor = j.status === 'completed' ? '#10b981' : j.status === 'failed' ? '#ef4444' : '#f59e0b';
      return `
        <div style="font-size:0.8rem; padding:0.4rem 0; border-bottom:1px solid #262e42;">
          <strong>${escapeHtml(j.filename)}</strong>: <span style="color:${statusColor}">${j.status}</span>
          ${j.total_extracted ? `(${j.total_extracted} extracted)` : ''}
          ${j.error ? `<span style="color:#ef4444; font-size:0.75rem;"> — ${escapeHtml(j.error)}</span>` : ''}
        </div>
      `;
    }).join('');
  } catch (err) {
    console.error('Failed to load jobs:', err);
  }
}

function renderQuestions() {
  const container = document.getElementById('questionsContainer');
  let filtered = allQuestions;

  if (activeFilter === 'needs_review') {
    filtered = allQuestions.filter(q => q.needs_human_review);
  } else if (activeFilter === 'valid') {
    filtered = allQuestions.filter(q => q.validation_status === 'valid');
  } else if (activeFilter === 'recheck_passed') {
    filtered = allQuestions.filter(q => q.recheck_verified === true);
  }

  if (!filtered.length) {
    container.innerHTML = `
      <div class="empty-state-large">
        <span class="empty-icon">🔍</span>
        <h3>No questions match filter</h3>
      </div>
    `;
    return;
  }

  container.innerHTML = filtered.map((q, idx) => {
    const isValid = q.validation_status === 'valid';
    const isRechecked = q.recheck_verified === true;
    const hasRecheckIssue = q.recheck_verified === false;

    return `
      <div class="question-card" id="qcard-${q.id}">
        <div class="card-top">
          <span style="font-size:0.85rem; font-weight:600; color:#9ca3af;">#${q.question_number || (idx + 1)} · ${q.source_file || 'PDF'} · Path: ${q.label_path || q.question_number}</span>
          <div style="display:flex; gap:0.5rem; align-items:center;">
            ${isRechecked ? '<span class="status-tag status-verified">🔍 AI Rechecked</span>' : ''}
            ${hasRecheckIssue ? '<span class="status-tag status-warning">⚠️ Recheck Flagged</span>' : ''}
            <span class="status-tag ${isValid ? 'status-valid' : 'status-review'}">
              ${isValid ? '✅ Valid KaTeX' : '⚠️ ' + (q.validation_errors?.[0] || 'Needs Review')}
            </span>
          </div>
        </div>

        ${q.context_latex ? `<div class="context-stimulus math-content"><em>${escapeHtml(q.context_latex)}</em></div>` : ''}

        <div class="stem-text math-content">${escapeHtml(q.reworded_stem || q.raw_stem)}</div>

        ${q.diagram_file ? `
          <div class="diagram-preview-card">
            <img src="/diagrams/${q.diagram_file}" alt="Cropped Geometry/Figure Diagram" class="diagram-img" onerror="this.style.display='none'">
            <div class="diagram-caption">📐 Cropped Figure: ${escapeHtml(q.diagram_file)}</div>
          </div>
        ` : ''}

        <div class="choices-list">
          ${(q.choices || []).map((c, i) => `
            <div class="choice-box ${q.correct_choice_index === i ? 'correct' : ''} math-content">
              <strong>(${String.fromCharCode(65 + i)})</strong> ${escapeHtml(c)}
            </div>
          `).join('')}
        </div>

        ${q.explanation_latex ? `
          <div class="explanation-box math-content">
            <strong>💡 Answer Key / Solution:</strong> ${escapeHtml(q.explanation_latex)}
          </div>
        ` : ''}

        ${q.recheck_solution ? `
          <div class="recheck-box math-content">
            <strong>🤖 Independent AI Solution:</strong> ${escapeHtml(q.recheck_solution)}
          </div>
        ` : ''}

        ${q.diagram_description ? `<div style="font-size:0.8rem; color:#9ca3af; margin-bottom:0.75rem;"><strong>Diagram Description:</strong> ${escapeHtml(q.diagram_description)}</div>` : ''}
        ${q.extraction_notes ? `<div style="font-size:0.75rem; color:#6b7280; margin-bottom:0.75rem;"><em>${escapeHtml(q.extraction_notes)}</em></div>` : ''}

        <div class="card-actions">
          <button class="btn btn-secondary" style="font-size:0.8rem; padding:0.3rem 0.6rem;" onclick="openEditModal('${q.id}')">✏️ Edit</button>
        </div>
      </div>
    `;
  }).join('');

  // Trigger KaTeX auto-render across all question elements
  if (window.renderMathInElement) {
    renderMathInElement(container, {
      delimiters: [
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false }
      ],
      throwOnError: false
    });
  }
}

function openEditModal(qId) {
  const q = allQuestions.find(x => x.id === qId);
  if (!q) return;

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

function closeModal() {
  document.getElementById('editModal').style.display = 'none';
}

async function saveQuestionEdit() {
  const qId = document.getElementById('editQuestionId').value;
  const q = allQuestions.find(x => x.id === qId);
  if (!q) return;

  const stem = document.getElementById('editStemInput').value;
  const c0 = document.getElementById('editChoice0').value;
  const c1 = document.getElementById('editChoice1').value;
  const c2 = document.getElementById('editChoice2').value;
  const c3 = document.getElementById('editChoice3').value;
  const corrIdxVal = document.getElementById('editCorrectIndex').value;
  const diagDesc = document.getElementById('editDiagramDesc').value;

  q.reworded_stem = stem;
  q.choices = [c0, c1, c2, c3].filter(x => x.trim() !== '');
  q.correct_choice_index = corrIdxVal !== '' ? parseInt(corrIdxVal) : null;
  q.diagram_description = diagDesc || null;

  try {
    const res = await fetch(`/api/questions/${qId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(q),
    });
    const data = await res.json();
    if (data.ok) {
      closeModal();
      fetchQuestions();
    }
  } catch (err) {
    alert('Failed to save edit: ' + err.message);
  }
}

function renderKaTeXIn(el, text) {
  el.textContent = text;
  if (window.renderMathInElement) {
    renderMathInElement(el, {
      delimiters: [
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false }
      ],
      throwOnError: false
    });
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
