"use strict";

const AUTH_TOKEN = document.body.dataset.authToken;
const MAX_OCR_PROMPT_BYTES = 64 * 1024;
const MAX_TRANSLATION_PROMPT_BYTES = 64 * 1024;
const MAX_SOURCE_UPLOAD_BYTES = 500 * 1024 * 1024;
const reviewLabels = {
  source: "원문 검수",
  glossary: "용어 검수",
  translation: "번역 검수",
};
let dashboard = null;
let activeFilter = "all";
let toastTimer = null;
let aiSettings = null;
let aiModelCatalog = null;
let aiModelCatalogs = {};
let sourceJobs = new Map();
let glossaryJobs = new Map();
let translationJobs = new Map();
let sourceJobPollTimer = null;
let pendingSourceJobProject = null;
let pendingGlossaryJobProject = null;
let pendingTranslationJobProject = null;
let pendingSourceProject = null;
let pendingSourceFilesProject = null;
let pendingOcrPromptProject = null;
let pendingTranslationPromptProject = null;
let pendingDeleteProject = null;
let pendingTranslationForce = false;

const byId = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const isFormData = options.body instanceof FormData;
  const response = await fetch(path, {
    ...options,
    headers: {
      "X-GLK-Token": AUTH_TOKEN,
      ...(options.body && !isFormData
        ? {"Content-Type": "application/json"} : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(payload?.message || "요청을 처리하지 못했습니다.");
  }
  return payload;
}

function showToast(message, isError = false) {
  const toast = byId("toast");
  const openDialogs = document.querySelectorAll("dialog[open]");
  const toastHost = openDialogs.length
    ? openDialogs[openDialogs.length - 1]
    : document.body;
  toastHost.append(toast);
  toast.textContent = message;
  toast.className = `toast show${isError ? " error" : ""}`;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toast.className = "toast";
    if (toast.parentElement !== document.body) {
      document.body.append(toast);
    }
  }, 3500);
}

function aiSettingSourceLabel(source) {
  return {
    environment: "셸 환경변수",
    env_file: ".env",
    default: "프로그램 기본값",
    missing: "미설정",
  }[source] || source;
}

function aiSettingSourceDescription(source) {
  return source === "default"
    ? "프로그램 기본값 사용 중"
    : `${aiSettingSourceLabel(source)}에서 적용 중`;
}

function renderAiSettings() {
  if (!aiSettings) return;
  const providerLabel = aiSettings.provider === "openai"
    ? "OpenAI"
    : "Google Gemini";
  const keyLabel = aiSettings.api_key_configured
    ? "API 키 설정 완료"
    : "API 키 미설정";
  byId("aiKeySummary").textContent = keyLabel;
  byId("aiKeySummary").classList.toggle(
    "missing",
    !aiSettings.api_key_configured,
  );
  byId("aiModelSummary").textContent = aiSettings.model;
  byId("aiProviderStatus").textContent = providerLabel;
  byId("aiProviderSource").textContent =
    aiSettingSourceDescription(aiSettings.provider_source);
  byId("aiApiKeyStatus").textContent = keyLabel;
  byId("aiApiKeySource").textContent = aiSettings.api_key_configured
    ? aiSettingSourceDescription(aiSettings.api_key_source)
    : `${providerLabel} 작업 전에 키를 입력하세요`;
  byId("aiModelStatus").textContent = aiSettings.model;
  byId("aiModelSource").textContent =
    aiSettingSourceDescription(aiSettings.model_source);
  const overridden = Object.values(
    aiSettings.environment_override || {},
  ).some(Boolean);
  byId("aiEnvironmentNote").classList.toggle("visible", overridden);
  render();
}

function renderAiModelOptions() {
  const select = byId("aiModelPreset");
  select.replaceChildren();
  for (const model of aiModelCatalog?.models || []) {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = model.id;
    select.append(option);
  }
  const custom = document.createElement("option");
  custom.value = "custom";
  custom.textContent = "직접 입력";
  select.append(custom);
}

async function loadAiSettings(silent = false) {
  try {
    const result = await api("/api/settings/ai");
    aiSettings = result.settings;
    aiModelCatalogs = result.model_catalogs || {
      [aiSettings.provider]: result.model_catalog,
    };
    aiModelCatalog = aiModelCatalogs[aiSettings.provider]
      || result.model_catalog;
    renderAiModelOptions();
    renderAiSettings();
    return aiSettings;
  } catch (error) {
    byId("aiKeySummary").textContent = "AI 설정 확인 실패";
    byId("aiKeySummary").classList.add("missing");
    byId("aiModelSummary").textContent = "서버 연결을 확인하세요";
    if (!silent) showToast(error.message, true);
    throw error;
  }
}

function updateAiModelInput() {
  const custom = byId("aiModelPreset").value === "custom";
  byId("aiCustomModelField").hidden = !custom;
  byId("aiCustomModel").required = custom;
  const selected = aiModelCatalog?.models?.find(
    (model) => model.id === byId("aiModelPreset").value,
  );
  byId("aiModelDescription").textContent = custom
    ? "목록에 없는 실제 API 모델 ID를 입력합니다."
    : selected?.description_ko || "";
}

function updateAiProviderInput({selectRecommended = true} = {}) {
  const provider = byId("aiProvider").value;
  const providerLabel = provider === "openai" ? "OpenAI" : "Gemini";
  aiModelCatalog = aiModelCatalogs[provider] || null;
  byId("aiApiKeyLabel").textContent = `${providerLabel} API 키`;
  byId("aiModelLabel").textContent = `${providerLabel} 모델`;
  renderAiModelOptions();
  if (selectRecommended) {
    const recommended = aiModelCatalog?.models?.find(
      (model) => model.recommended,
    );
    byId("aiModelPreset").value = recommended?.id || "custom";
    byId("aiCustomModel").value = "";
  }
  updateAiModelInput();
}

async function openAiSettingsDialog() {
  const button = byId("aiSettingsButton");
  button.disabled = true;
  try {
    await loadAiSettings();
    byId("aiProvider").value = aiSettings.provider;
    updateAiProviderInput({selectRecommended: false});
    const presets = (aiModelCatalog?.models || []).map(
      (model) => model.id,
    );
    const isPreset = presets.includes(aiSettings.model);
    byId("aiModelPreset").value =
      isPreset ? aiSettings.model : "custom";
    byId("aiCustomModel").value = isPreset ? "" : aiSettings.model;
    byId("aiApiKey").value = "";
    byId("aiApiKey").type = "password";
    byId("aiApiKeyToggle").textContent = "보기";
    updateAiModelInput();
    byId("aiSettingsDialog").showModal();
    byId("aiApiKey").focus();
  } catch {
    // loadAiSettings already displays a localized error.
  } finally {
    button.disabled = false;
  }
}

function closeAiSettingsDialog() {
  byId("aiSettingsDialog").close();
}

function selectedAiModel() {
  return byId("aiModelPreset").value === "custom"
    ? byId("aiCustomModel").value.trim()
    : byId("aiModelPreset").value;
}

async function saveAiSettings(event) {
  event.preventDefault();
  const model = selectedAiModel();
  if (!model) {
    showToast("사용할 AI 모델 이름을 입력하세요.", true);
    byId("aiCustomModel").focus();
    return;
  }
  const submit = byId("aiSettingsSubmit");
  submit.disabled = true;
  try {
    const result = await api("/api/settings/ai", {
      method: "PUT",
      body: JSON.stringify({
        provider: byId("aiProvider").value,
        api_key: byId("aiApiKey").value,
        model,
      }),
    });
    aiSettings = result.settings;
    renderAiSettings();
    closeAiSettingsDialog();
    showToast("AI 설정을 저장했습니다.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
  }
}

function sourceJobFor(projectId) {
  return sourceJobs.get(projectId) || null;
}

function activeSourceJob() {
  return [...sourceJobs.values()].find(
    (job) => ["queued", "running"].includes(job.status),
  ) || null;
}

function sourceJobIsActive(projectId) {
  const job = sourceJobFor(projectId);
  return Boolean(
    job && ["queued", "running"].includes(job.status),
  );
}

function glossaryJobFor(projectId) {
  return glossaryJobs.get(projectId) || null;
}

function activeGlossaryJob() {
  return [...glossaryJobs.values()].find(
    (job) => ["queued", "running"].includes(job.status),
  ) || null;
}

function glossaryJobIsActive(projectId) {
  const job = glossaryJobFor(projectId);
  return Boolean(
    job && ["queued", "running"].includes(job.status),
  );
}

function translationJobFor(projectId) {
  return translationJobs.get(projectId) || null;
}

function activeTranslationJob() {
  return [...translationJobs.values()].find(
    (job) => ["queued", "running"].includes(job.status),
  ) || null;
}

function translationJobIsActive(projectId) {
  const job = translationJobFor(projectId);
  return Boolean(
    job && ["queued", "running"].includes(job.status),
  );
}

function activeBackgroundJob() {
  return activeSourceJob()
    || activeGlossaryJob()
    || activeTranslationJob();
}

function projectJobIsActive(projectId) {
  return sourceJobIsActive(projectId)
    || glossaryJobIsActive(projectId)
    || translationJobIsActive(projectId);
}

function jobUsage(job) {
  const usage = job?.result?.usage;
  return usage && Number.isInteger(usage.requests) && usage.requests > 0
    ? usage
    : null;
}

function formatTokenCount(value) {
  if (!Number.isInteger(value)) return "0";
  return new Intl.NumberFormat("ko-KR", {notation: "compact"}).format(value);
}

function jobElapsedLabel(job) {
  const started = Date.parse(job.started_at || "");
  const ended = Date.parse(job.finished_at || job.updated_at || "");
  if (!Number.isFinite(started) || !Number.isFinite(ended)) return "";
  const seconds = Math.max(0, Math.round((ended - started) / 1000));
  if (seconds < 60) return `${seconds}초`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes}분 ${remainder}초` : `${minutes}분`;
}

function jobDetailMeta(job) {
  const values = [];
  if (
    Number.isInteger(job.progress_current)
    && Number.isInteger(job.progress_total)
    && job.progress_total > 0
  ) {
    const current = Math.min(job.progress_current, job.progress_total);
    const percent = Math.round(current / job.progress_total * 100);
    values.push(`${current} / ${job.progress_total} · ${percent}%`);
  }
  const elapsed = jobElapsedLabel(job);
  if (elapsed) values.push(`소요 ${elapsed}`);
  const usage = jobUsage(job);
  if (usage) {
    values.push(
      `요청 ${usage.requests}회 · 입력 ${formatTokenCount(usage.input_tokens)}`
      + ` · 출력 ${formatTokenCount(usage.output_tokens)}`,
    );
    if (Number.isFinite(usage.estimated_cost_usd)) {
      values.push(`예상 비용 $${usage.estimated_cost_usd.toFixed(4)}`);
    } else {
      values.push("예상 비용 단가 미등록");
    }
  }
  return values.length
    ? `<div class="job-meta">${values.map(
        (value) => `<span>${escapeHtml(value)}</span>`,
      ).join("")}</div>`
    : "";
}

function jobFailureDetail(job) {
  const details = job?.result?.failure_details;
  if (!Array.isArray(details) || !details.length) return "";
  return `<details class="job-failure-details">
    <summary>실패 항목 ${details.length}개</summary>
    <ul>${details.map((detail) => `<li>
      <strong>${escapeHtml(detail.item || "항목")}</strong>
      <span>${escapeHtml(detail.message || "처리하지 못했습니다.")}</span>
    </li>`).join("")}</ul>
  </details>`;
}

function sourceJobStatusLabel(status) {
  return {
    queued: "실행 대기",
    running: "원문 준비 중",
    succeeded: "원문 준비 완료",
    partial: "일부 처리 실패",
    failed: "실행 실패",
    interrupted: "실행 중단",
  }[status] || "상태 확인 필요";
}

function sourceJobStatus(project) {
  const job = sourceJobFor(project.project_id);
  if (!job || (job.status === "succeeded" && !jobUsage(job))) return "";
  const active = ["queued", "running"].includes(job.status);
  const failed = ["partial", "failed", "interrupted"].includes(
    job.status,
  );
  if (!active && !failed && job.status !== "succeeded") return "";
  const progress = (
    Number.isInteger(job.progress_current)
    && Number.isInteger(job.progress_total)
    && job.progress_total > 0
  )
    ? `<progress class="job-progress"
        value="${Math.min(job.progress_current, job.progress_total)}"
        max="${job.progress_total}"></progress>`
    : "";
  const error = job.error
    ? `<p class="job-error">${escapeHtml(job.error)}</p>`
    : "";
  return `<div class="job-status${failed ? " failed" : ""}">
    <div class="job-status-head">
      <span>${sourceJobStatusLabel(job.status)}</span>
      <span>${escapeHtml(job.model)}</span>
    </div>
    ${progress}
    <p class="job-status-message">
      ${escapeHtml(job.progress_message || "")}
    </p>
    ${jobDetailMeta(job)}
    ${error}
    ${jobFailureDetail(job)}
  </div>`;
}

function sourceJobButton(project) {
  if (
    !["pdf", "images"].includes(project.source_type)
    || project.reviews.source.enabled
  ) {
    return "";
  }
  const active = activeBackgroundJob();
  if (sourceJobIsActive(project.project_id)) return "";
  if (active) {
    return `<button class="${actionClass(project, "source-job", "source-job-button")}" type="button" disabled
      title="${escapeHtml(active.project_id)} 백그라운드 작업이 실행 중입니다.">
      다른 백그라운드 작업 실행 중
    </button>`;
  }
  if (!aiSettings?.api_key_configured) {
    return `<button class="${actionClass(project, "source-job", "source-job-button")}" type="button"
      data-require-ai-settings>
      AI 설정 후 원문 준비
    </button>`;
  }
  const previous = sourceJobFor(project.project_id);
  const retry = (
    project.pipeline.source_processing_started
    || ["partial", "failed", "interrupted"].includes(previous?.status)
  );
  const action = project.source_type === "pdf"
    ? "PDF 원문 준비"
    : "이미지 OCR 및 원문 준비";
  return `<button class="${actionClass(project, "source-job", "source-job-button")}" type="button"
    data-start-source-job="${escapeHtml(project.project_id)}">
    ${action}${retry ? " 다시 실행" : " 시작"}
  </button>`;
}

function glossaryJobStatus(project) {
  const job = glossaryJobFor(project.project_id);
  if (!job || job.status === "succeeded") return "";
  const active = ["queued", "running"].includes(job.status);
  const failed = ["failed", "interrupted"].includes(job.status);
  if (!active && !failed) return "";
  const progress = (
    Number.isInteger(job.progress_current)
    && Number.isInteger(job.progress_total)
    && job.progress_total > 0
  )
    ? `<progress class="job-progress"
        value="${Math.min(job.progress_current, job.progress_total)}"
        max="${job.progress_total}"></progress>`
    : "";
  const error = job.error
    ? `<p class="job-error">${escapeHtml(job.error)}</p>`
    : "";
  const label = {
    queued: "용어 후보 생성 대기",
    running: "용어 후보 생성 중",
    failed: "용어 후보 생성 실패",
    interrupted: "용어 후보 생성 중단",
  }[job.status] || "상태 확인 필요";
  return `<div class="job-status${failed ? " failed" : ""}">
    <div class="job-status-head">
      <span>${label}</span>
      <span>로컬 작업</span>
    </div>
    ${progress}
    <p class="job-status-message">
      ${escapeHtml(job.progress_message || "")}
    </p>
    ${jobDetailMeta(job)}
    ${error}
    ${jobFailureDetail(job)}
  </div>`;
}

function glossaryJobButton(project) {
  if (
    !project.pipeline.final_source_approved
    || project.pipeline.glossary_status === "current"
    || glossaryJobIsActive(project.project_id)
  ) {
    return "";
  }
  if (project.pipeline.glossary_status === "stale") {
    return `<button class="${actionClass(project, "glossary-job", "glossary-job-button")}" type="button" disabled
      title="기존 용어 검수 편집을 보호하기 위해 대시보드에서 덮어쓰지 않습니다.">
      용어 후보 재생성은 CLI에서 확인
    </button>`;
  }
  const active = activeBackgroundJob();
  if (active) {
    return `<button class="${actionClass(project, "glossary-job", "glossary-job-button")}" type="button" disabled
      title="${escapeHtml(active.project_id)} 백그라운드 작업이 실행 중입니다.">
      다른 백그라운드 작업 실행 중
    </button>`;
  }
  const previous = glossaryJobFor(project.project_id);
  const retry = ["failed", "interrupted"].includes(previous?.status);
  return `<button class="${actionClass(project, "glossary-job", "glossary-job-button")}" type="button"
    data-start-glossary-job="${escapeHtml(project.project_id)}">
    용어 후보 생성${retry ? " 다시 시도" : " 시작"}
  </button>`;
}

function sourceDownloadButton(project) {
  const output = project.source_output;
  if (!output) return "";
  return `<button class="source-download-button secondary-action" type="button"
    data-download-source
    data-project="${escapeHtml(project.project_id)}"
    data-download-name="${escapeHtml(output.download_name)}"
    title="저장 위치를 선택해 다운로드">
    검수 완료 원문 TXT 저장
  </button>`;
}

function translationJobStatus(project) {
  const job = translationJobFor(project.project_id);
  if (
    !job
    || (job.status === "succeeded" && !jobUsage(job))
    || (project.pipeline.translation_status === "current" && !jobUsage(job))
  ) return "";
  const active = ["queued", "running"].includes(job.status);
  const failed = ["failed", "interrupted"].includes(job.status);
  if (!active && !failed && job.status !== "succeeded") return "";
  const progress = (
    Number.isInteger(job.progress_current)
    && Number.isInteger(job.progress_total)
    && job.progress_total > 0
  )
    ? `<progress class="job-progress"
        value="${Math.min(job.progress_current, job.progress_total)}"
        max="${job.progress_total}"></progress>`
    : "";
  const error = job.error
    ? `<p class="job-error">${escapeHtml(job.error)}</p>`
    : "";
  const label = {
    queued: "초벌 번역 대기",
    running: "초벌 번역 중",
    succeeded: "초벌 번역 완료",
    failed: "초벌 번역 실패",
    interrupted: "초벌 번역 중단",
  }[job.status] || "상태 확인 필요";
  return `<div class="job-status${failed ? " failed" : ""}">
    <div class="job-status-head">
      <span>${label}</span>
      <span>${escapeHtml(job.model)}</span>
    </div>
    ${progress}
    <p class="job-status-message">
      ${escapeHtml(job.progress_message || "")}
    </p>
    ${jobDetailMeta(job)}
    ${error}
    ${jobFailureDetail(job)}
  </div>`;
}

function translationReviewAttention(project) {
  if (project.stage !== "translation_qa_failed") return "";
  const count = project.pipeline.translation_qa_issues;
  const countLabel = Number.isInteger(count)
    ? `${count}개 오류`
    : "확인 필요 항목";
  return `<div class="translation-review-attention" role="status">
    <strong>초벌 번역 완료 · ${escapeHtml(countLabel)}</strong>
    <span>번역 결과는 보존했습니다. 번역 검수에서 표시된 블록을 수정한 뒤 다시 검사하세요.</span>
  </div>`;
}

function translationJobButton(project) {
  const status = project.pipeline.translation_status;
  if (
    project.pipeline.termbase_status !== "current"
    || status === "current"
    || translationJobIsActive(project.project_id)
  ) {
    return "";
  }
  if (!["not_run", "partial", "stale"].includes(status)) return "";
  const active = activeBackgroundJob();
  if (active) {
    return `<button class="${actionClass(project, "translation-job", "translation-job-button")}" type="button" disabled
      title="${escapeHtml(active.project_id)} 백그라운드 작업이 실행 중입니다.">
      다른 백그라운드 작업 실행 중
    </button>`;
  }
  if (!aiSettings?.api_key_configured) {
    return `<button class="${actionClass(project, "translation-job", "translation-job-button")}" type="button"
      data-require-ai-settings>
      AI 설정 후 초벌 번역
    </button>`;
  }
  const resume = status === "partial";
  const force = status === "stale";
  return `<button class="${actionClass(project, "translation-job", "translation-job-button")}" type="button"
    data-start-translation-job="${escapeHtml(project.project_id)}">
    ${force
      ? "변경된 프롬프트로 전체 재번역"
      : resume
      ? "초벌 번역 이어서 실행"
      : "초벌 번역 시작"}
  </button>`;
}

function setSourceJobs(jobs) {
  sourceJobs = new Map(
    jobs.map((job) => [job.project_id, job]),
  );
}

function setGlossaryJobs(jobs) {
  glossaryJobs = new Map(
    jobs.map((job) => [job.project_id, job]),
  );
}

function setTranslationJobs(jobs) {
  translationJobs = new Map(
    jobs.map((job) => [job.project_id, job]),
  );
}

function scheduleSourceJobPoll() {
  window.clearTimeout(sourceJobPollTimer);
  sourceJobPollTimer = null;
  if (activeBackgroundJob()) {
    sourceJobPollTimer = window.setTimeout(pollSourceJobs, 1000);
  }
}

async function pollSourceJobs() {
  const previousSource = new Map(sourceJobs);
  const previousGlossary = new Map(glossaryJobs);
  const previousTranslation = new Map(translationJobs);
  try {
    const result = await api("/api/jobs");
    setSourceJobs(result.jobs || []);
    setGlossaryJobs(result.glossary_jobs || []);
    setTranslationJobs(result.translation_jobs || []);
    const completedSource = [...sourceJobs.values()].find((job) => {
      const before = previousSource.get(job.project_id);
      return (
        before
        && ["queued", "running"].includes(before.status)
        && !["queued", "running"].includes(job.status)
      );
    });
    const completedGlossary = [...glossaryJobs.values()].find((job) => {
      const before = previousGlossary.get(job.project_id);
      return (
        before
        && ["queued", "running"].includes(before.status)
        && !["queued", "running"].includes(job.status)
      );
    });
    const completedTranslation = [...translationJobs.values()].find(
      (job) => {
        const before = previousTranslation.get(job.project_id);
        return (
          before
          && ["queued", "running"].includes(before.status)
          && !["queued", "running"].includes(job.status)
        );
      },
    );
    if (completedSource || completedGlossary || completedTranslation) {
      dashboard = await api("/api/dashboard");
    }
    if (completedSource) {
      const message = completedSource.status === "succeeded"
        ? "원문 검수 준비가 완료되었습니다."
        : (completedSource.error || completedSource.progress_message);
      showToast(
        message,
        completedSource.status !== "succeeded",
      );
    }
    if (completedGlossary) {
      const count = completedGlossary.result
        ?.glossary?.candidate_count;
      const message = completedGlossary.status === "succeeded"
        ? `용어 후보 ${Number.isInteger(count) ? `${count}개 ` : ""}생성이 완료되었습니다.`
        : (completedGlossary.error || completedGlossary.progress_message);
      showToast(
        message,
        completedGlossary.status !== "succeeded",
      );
    }
    if (completedTranslation) {
      const blocks = completedTranslation.result
        ?.translation?.completed_blocks;
      const message = completedTranslation.status === "succeeded"
        ? `초벌 번역 ${Number.isInteger(blocks) ? `${blocks}개 블록 ` : ""}생성이 완료되었습니다.`
        : (
          completedTranslation.error
          || completedTranslation.progress_message
        );
      showToast(
        message,
        completedTranslation.status !== "succeeded",
      );
    }
    render();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    scheduleSourceJobPoll();
  }
}

function openSourceJobDialog(projectId) {
  if (!aiSettings?.api_key_configured) {
    showToast("먼저 AI API 키를 설정하세요.", true);
    openAiSettingsDialog();
    return;
  }
  const project = dashboard?.projects.find(
    (value) => value.project_id === projectId,
  );
  if (!project) {
    showToast("프로젝트 정보를 찾지 못했습니다.", true);
    return;
  }
  pendingSourceJobProject = project;
  byId("sourceJobProjectName").textContent = project.name;
  byId("sourceJobProjectId").textContent = project.project_id;
  byId("sourceJobType").textContent =
    project.source_type === "pdf" ? "PDF" : "이미지 OCR";
  byId("sourceJobModel").textContent = aiSettings.model;
  const showsOcrPrompt = project.source_type === "images";
  byId("sourceJobOcrPromptField").hidden = !showsOcrPrompt;
  byId("sourceJobOcrPrompt").value =
    showsOcrPrompt ? project.ocr_prompt || "" : "";
  byId("sourceJobSubmit").textContent =
    project.source_type === "pdf"
      ? "PDF 원문 준비 시작"
      : "이미지 OCR 시작";
  byId("sourceJobDialog").showModal();
  byId("sourceJobDialogCancel").focus();
}

function closeSourceJobDialog() {
  byId("sourceJobDialog").close();
}

async function startSourceJob(event) {
  event.preventDefault();
  if (!pendingSourceJobProject) return;
  const submit = byId("sourceJobSubmit");
  submit.disabled = true;
  try {
    const result = await api("/api/jobs/source", {
      method: "POST",
      body: JSON.stringify({
        project_id: pendingSourceJobProject.project_id,
      }),
    });
    sourceJobs.set(result.job.project_id, result.job);
    closeSourceJobDialog();
    render();
    scheduleSourceJobPoll();
    showToast("원문 준비 작업을 시작했습니다.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
  }
}

function openGlossaryJobDialog(projectId) {
  const project = dashboard?.projects.find(
    (value) => value.project_id === projectId,
  );
  if (!project) {
    showToast("프로젝트 정보를 찾지 못했습니다.", true);
    return;
  }
  if (!project.pipeline.final_source_approved) {
    showToast("먼저 원문을 최종 승인하세요.", true);
    return;
  }
  pendingGlossaryJobProject = project;
  byId("glossaryJobProjectName").textContent = project.name;
  byId("glossaryJobProjectId").textContent = project.project_id;
  byId("glossaryJobDialog").showModal();
  byId("glossaryJobDialogCancel").focus();
}

function closeGlossaryJobDialog() {
  byId("glossaryJobDialog").close();
}

async function startGlossaryJob(event) {
  event.preventDefault();
  if (!pendingGlossaryJobProject) return;
  const submit = byId("glossaryJobSubmit");
  submit.disabled = true;
  try {
    const result = await api("/api/jobs/glossary", {
      method: "POST",
      body: JSON.stringify({
        project_id: pendingGlossaryJobProject.project_id,
      }),
    });
    glossaryJobs.set(result.job.project_id, result.job);
    closeGlossaryJobDialog();
    render();
    scheduleSourceJobPoll();
    showToast("용어 후보 생성 작업을 시작했습니다.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
  }
}

function updateTranslationPromptCount() {
  const value = byId("translationPrompt").value;
  const byteCount = new TextEncoder().encode(value).length;
  const overLimit = byteCount > MAX_TRANSLATION_PROMPT_BYTES;
  const count = byId("translationPromptCount");
  count.textContent =
    `${byteCount.toLocaleString()} / ${MAX_TRANSLATION_PROMPT_BYTES.toLocaleString()} bytes`;
  count.classList.toggle("limit", overLimit);
  byId("translationJobSubmit").disabled =
    overLimit || !value.trim();
}

function openTranslationJobDialog(projectId) {
  if (!aiSettings?.api_key_configured) {
    showToast("먼저 AI API 키를 설정하세요.", true);
    openAiSettingsDialog();
    return;
  }
  const project = dashboard?.projects.find(
    (value) => value.project_id === projectId,
  );
  if (!project) {
    showToast("프로젝트 정보를 찾지 못했습니다.", true);
    return;
  }
  if (project.pipeline.termbase_status !== "current") {
    showToast("먼저 용어집을 확정하세요.", true);
    return;
  }
  const resume = project.pipeline.translation_status === "partial";
  const force = project.pipeline.translation_status === "stale";
  pendingTranslationJobProject = project;
  pendingTranslationForce = force;
  byId("translationJobProjectName").textContent = project.name;
  byId("translationJobProjectId").textContent = project.project_id;
  byId("translationJobModel").textContent = aiSettings.model;
  byId("translationJobMode").textContent =
    force
      ? "기존 결과 보관 후 전체 재번역"
      : resume
      ? "저장된 청크부터 이어하기"
      : "새 초벌 번역";
  byId("translationPrompt").value =
    project.translation_prompt?.value || "";
  byId("translationPrompt").readOnly = true;
  byId("translationResumeNote").hidden = !resume;
  byId("translationForceNote").hidden = !force;
  byId("translationJobSubmit").textContent =
    force
      ? "전체 재번역 시작"
      : resume
      ? "초벌 번역 이어서 실행"
      : "초벌 번역 시작";
  updateTranslationPromptCount();
  byId("translationJobDialog").showModal();
  byId("translationJobDialogCancel").focus();
}

function closeTranslationJobDialog() {
  byId("translationJobDialog").close();
}

async function startTranslationJob(event) {
  event.preventDefault();
  if (!pendingTranslationJobProject) return;
  const submit = byId("translationJobSubmit");
  submit.disabled = true;
  try {
    const result = await api("/api/jobs/translation", {
      method: "POST",
      body: JSON.stringify({
        project_id: pendingTranslationJobProject.project_id,
        prompt: byId("translationPrompt").value,
        force: pendingTranslationForce,
      }),
    });
    translationJobs.set(result.job.project_id, result.job);
    closeTranslationJobDialog();
    render();
    scheduleSourceJobPoll();
    showToast(
      result.job.resume
        ? "저장된 초벌 번역을 이어서 실행합니다."
        : result.job.force
        ? "기존 결과를 보관하고 전체 재번역을 시작했습니다."
        : "초벌 번역 작업을 시작했습니다.",
    );
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
  }
}

function sourceTypeLabel(project) {
  const type = project.source_type;
  const count = project.source_files?.length || 0;
  if (type === "images") return count ? `Images · ${count}개` : "Images";
  if (type === "pdf") return "PDF";
  if (type === "mixed") return count ? `Mixed · ${count}개` : "Mixed";
  return "대기";
}

function stepState(project, key) {
  const p = project.pipeline;
  const currentAction = primaryActionKey(project);
  const currentStep = currentAction.startsWith("source-")
    ? (currentAction === "source-review" ? "review" : "source")
    : currentAction.startsWith("glossary-")
    ? "glossary"
    : currentAction.startsWith("translation-")
    ? "translation"
    : "";
  if (key === "source") {
    if (p.review_source_ready || p.final_source_approved) return "done";
  }
  if (key === "review") {
    if (p.final_source_approved) return "done";
  }
  if (key === "glossary") {
    if (p.termbase_status === "current") return "done";
  }
  if (key === "translation" && p.final_translation_approved) return "done";
  return key === currentStep ? "current" : "";
}

function pipelineStep(project, key, label) {
  const state = stepState(project, key);
  const stateLabel = state === "done"
    ? "완료"
    : state === "current"
    ? "현재"
    : "대기";
  return `<span class="pipeline-step ${state}">
    <span>${label}</span>
    <span class="pipeline-step-state">${stateLabel}</span>
  </span>`;
}

function projectNeedsAttention(project) {
  return project.stage === "translation_qa_failed" || !project.workspace_ready;
}

function primaryActionKey(project) {
  const pipeline = project.pipeline;
  if (pipeline.final_translation_approved) return "";
  if (!project.source_type) return "source-registration";
  if (!pipeline.final_source_approved) {
    return project.reviews.source.enabled ? "source-review" : "source-job";
  }
  if (pipeline.termbase_status !== "current") {
    return project.reviews.glossary.enabled
      ? "glossary-review"
      : "glossary-job";
  }
  return project.reviews.translation.enabled
    ? "translation-review"
    : "translation-job";
}

function actionClass(project, key, baseClass) {
  const hierarchy = primaryActionKey(project) === key
    ? "primary-action"
    : "secondary-action";
  return `${baseClass} ${hierarchy}`;
}

function matchesFilter(project) {
  if (activeFilter === "completed") {
    return project.pipeline.final_translation_approved;
  }
  if (activeFilter === "progress") {
    return project.stage !== "not_started"
      && !project.pipeline.final_translation_approved;
  }
  if (activeFilter === "attention") {
    return projectNeedsAttention(project);
  }
  return true;
}

function reviewButton(project, type) {
  const review = project.reviews[type];
  if (!review.enabled) return "";
  return `<button class="${actionClass(project, `${type}-review`, "review-button")}" type="button"
    data-project="${escapeHtml(project.project_id)}"
    data-review="${type}" title="${escapeHtml(review.reason)}">
    ${reviewLabels[type]}
  </button>`;
}

function sourceRegistrationButton(project) {
  if (sourceJobIsActive(project.project_id)) return "";
  if (project.source_type) {
    if (!project.source_replacement?.allowed) return "";
    return `<button class="source-register-button replace" type="button"
      data-register-source="${escapeHtml(project.project_id)}"
      data-project-name="${escapeHtml(project.name)}"
      data-source-type="${escapeHtml(project.source_type)}"
      data-replace-source="true"
      title="${escapeHtml(project.source_replacement.reason)}">
      원본 교체
    </button>`;
  }
  return `<button class="${actionClass(project, "source-registration", "source-register-button")}" type="button"
    data-register-source="${escapeHtml(project.project_id)}"
    data-project-name="${escapeHtml(project.name)}"
    data-replace-source="false">
    PDF 또는 이미지 원본 등록
  </button>`;
}

function ocrPromptEditButton(project) {
  if (
    project.source_type !== "images"
    || !project.ocr_prompt_edit?.allowed
    || sourceJobIsActive(project.project_id)
  ) {
    return "";
  }
  return `<button class="ocr-prompt-button secondary-action" type="button"
    data-edit-ocr-prompt="${escapeHtml(project.project_id)}"
    title="${escapeHtml(project.ocr_prompt_edit.reason)}">
    OCR 프롬프트 수정
  </button>`;
}

function translationPromptEditButton(project) {
  if (project.pipeline.termbase_status !== "current") return "";
  const active = activeBackgroundJob();
  const disabled = active ? " disabled" : "";
  const title = active
    ? `${active.project_id} 백그라운드 작업이 실행 중입니다.`
    : "AI API 호출 없이 프로젝트 번역 지침만 저장합니다.";
  const label = project.translation_prompt?.saved
    ? "번역 프롬프트 수정"
    : "번역 프롬프트 설정";
  return `<button class="translation-prompt-button secondary-action" type="button"
    data-edit-translation-prompt="${escapeHtml(project.project_id)}"
    title="${escapeHtml(title)}"${disabled}>
    ${label}
  </button>`;
}

function sourceFileSummary(project) {
  const files = project.source_files || [];
  if (!files.length) return "";
  const multipleImages =
    project.source_type === "images" && files.length > 1;
  const summary = multipleImages
    ? `${files[0]} 외 ${files.length - 1}개`
    : files[0];
  const listButton = multipleImages
    ? `<button class="source-file-list-button" type="button"
        data-source-files="${escapeHtml(project.project_id)}"
        aria-label="${escapeHtml(project.name)} 원본 파일 ${files.length}개 보기">
        파일 목록
      </button>`
    : "";
  return `<div class="source-file-row">
    <span class="source-file-summary" title="${escapeHtml(summary)}">
      ${escapeHtml(summary)}
    </span>
    ${listButton}
  </div>`;
}

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function outputFiles(project) {
  const outputs = project.outputs || [];
  if (!outputs.length) return "";
  const outputRow = (output, label) => `
    <div class="output-file">
      <div class="output-file-info">
        <span class="output-file-name" title="${escapeHtml(output.name)}">
          ${escapeHtml(output.name)}
        </span>
        <span class="output-file-size">
          ${escapeHtml(formatFileSize(output.size_bytes))}
        </span>
      </div>
      <button class="output-download-button" type="button"
        data-download-output="${escapeHtml(output.path)}"
        data-project="${escapeHtml(project.project_id)}"
        data-download-name="${escapeHtml(output.download_name)}"
        title="저장 위치를 선택해 다운로드"
        aria-label="${escapeHtml(output.name)} 저장 위치 선택">
        ${escapeHtml(label)}
      </button>
    </div>`;

  let rows;
  let summary;
  const combined = outputs.find(
    (output) => output.name === "combined_kor.txt",
  );
  const imageOutputs = outputs.filter(
    (output) => output.name !== "combined_kor.txt",
  );
  if (
    project.source_type === "images"
    && combined
    && imageOutputs.length
  ) {
    const archiveName =
      `${project.project_id}_image_outputs.zip`;
    const totalBytes = imageOutputs.reduce(
      (sum, output) => sum + output.size_bytes,
      0,
    );
    rows = outputRow(combined, "통합본 저장") + `
      <div class="output-file">
        <div class="output-file-info">
          <span class="output-file-name"
            title="이미지별 번역 파일 전체">
            이미지별 번역 파일
          </span>
          <span class="output-file-size">
            ${imageOutputs.length}개 TXT ·
            ${escapeHtml(formatFileSize(totalBytes))}
          </span>
        </div>
        <button class="output-download-button" type="button"
          data-download-output-archive
          data-project="${escapeHtml(project.project_id)}"
          data-download-name="${escapeHtml(archiveName)}"
          title="이미지별 파일을 ZIP으로 저장"
          aria-label="이미지별 파일 ${imageOutputs.length}개 전체 저장">
          이미지별 파일 전체 저장
        </button>
      </div>`;
    summary = `이미지별 파일 ${imageOutputs.length}개`;
  } else {
    rows = outputs.map(
      (output) => outputRow(output, "번역본 저장"),
    ).join("");
    summary = `${outputs.length}개 파일`;
  }
  return `<section class="output-files" aria-label="최종 번역 결과">
    <div class="output-files-head">
      <strong>최종 번역 결과</strong>
      <span>${escapeHtml(summary)}</span>
    </div>
    <div class="output-file-list">${rows}</div>
  </section>`;
}

function projectCard(project) {
  const jobLocked = projectJobIsActive(project.project_id);
  const attentionClass = projectNeedsAttention(project)
    ? " needs-attention"
    : "";
  return `<article class="project-card${attentionClass}">
    <div class="project-overview">
      <div class="project-head">
        <div>
          <h2 class="project-name">${escapeHtml(project.name)}</h2>
          <span class="project-id">${escapeHtml(project.project_id)}</span>
        </div>
        <div class="project-controls">
          <span class="source-badge">${sourceTypeLabel(project)}</span>
          <button class="delete-project-button" type="button"
            data-delete-project="${escapeHtml(project.project_id)}"
            data-project-name="${escapeHtml(project.name)}"
            aria-label="${escapeHtml(project.name)} 프로젝트 삭제"
            title="${jobLocked
              ? "백그라운드 작업 중에는 삭제할 수 없습니다."
              : "프로젝트 삭제"}"${jobLocked ? " disabled" : ""}>×</button>
        </div>
      </div>
      ${sourceFileSummary(project)}
    </div>
    <div class="project-status">
      <div class="stage-row">
        <span class="stage">${escapeHtml(project.stage_label)}</span>
        <span class="progress-value">${project.progress}%</span>
      </div>
      <div class="progress-track" aria-label="진행률 ${project.progress}%">
        <div class="progress-bar" style="width:${project.progress}%"></div>
      </div>
      <div class="pipeline" aria-label="작업 단계">
        ${pipelineStep(project, "source", "원문 추출")}
        ${pipelineStep(project, "review", "원문 검수")}
        ${pipelineStep(project, "glossary", "용어 정리")}
        ${pipelineStep(project, "translation", "번역 검수")}
      </div>
    </div>
    <div class="project-work">
      ${outputFiles(project)}
      <div class="actions">
        ${sourceJobStatus(project)}
        ${glossaryJobStatus(project)}
        ${translationJobStatus(project)}
        ${translationReviewAttention(project)}
        ${sourceJobButton(project)}
        ${glossaryJobButton(project)}
        ${sourceDownloadButton(project)}
        ${translationPromptEditButton(project)}
        ${translationJobButton(project)}
        ${ocrPromptEditButton(project)}
        ${reviewButton(project, "source")}
        ${reviewButton(project, "glossary")}
        ${reviewButton(project, "translation")}
        ${sourceRegistrationButton(project)}
      </div>
    </div>
  </article>`;
}

function createProjectCard() {
  return `<button class="create-project-card" type="button"
    data-create-project>
    <span class="create-mark" aria-hidden="true">+</span>
    <strong>새 프로젝트</strong>
    <span>번역 작업 공간 추가</span>
  </button>`;
}

function render() {
  if (!dashboard) return;
  const query = byId("searchInput").value.trim().toLocaleLowerCase("ko");
  const projects = dashboard.projects.filter((project) => {
    const haystack = `${project.name} ${project.project_id}`.toLocaleLowerCase("ko");
    return haystack.includes(query) && matchesFilter(project);
  });

  const summary = dashboard.summary;
  byId("summaryProjects").textContent = summary.projects;
  byId("summaryProgress").textContent = summary.in_progress;
  byId("summaryCompleted").textContent = summary.completed;
  byId("summaryAttention").textContent = summary.needs_attention;
  const hasAttention = Number(summary.needs_attention) > 0;
  byId("summaryAttention").closest(".summary-card").classList.toggle(
    "has-attention",
    hasAttention,
  );
  const attentionFilter = document.querySelector(
    '[data-filter="attention"]',
  );
  attentionFilter.classList.toggle("has-attention", hasAttention);
  byId("attentionFilterCount").textContent = summary.needs_attention;

  const warning = byId("warningBanner");
  if (dashboard.warnings.length) {
    warning.textContent =
      `읽지 못한 프로젝트 폴더가 ${dashboard.warnings.length}개 있습니다. ` +
      "터미널의 glk projects 결과에서 세부 내용을 확인하세요.";
    warning.style.display = "block";
  } else {
    warning.style.display = "none";
  }

  const cards = projects.map(projectCard);
  if (projects.length && activeFilter === "all" && !query) {
    cards.push(createProjectCard());
  }
  byId("projectGrid").innerHTML = cards.join("");
  const empty = byId("emptyState");
  empty.style.display = projects.length ? "none" : "block";
  if (!dashboard.projects.length) {
    byId("emptyCreateButton").style.display = "block";
    byId("emptyTitle").textContent = "아직 프로젝트가 없습니다";
    byId("emptyText").textContent =
      "새 프로젝트를 만들고 번역 작업을 시작하세요.";
  } else {
    byId("emptyCreateButton").style.display = "none";
    byId("emptyTitle").textContent = "조건에 맞는 프로젝트가 없습니다";
    byId("emptyText").textContent = "검색어나 상태 필터를 바꿔보세요.";
  }
}

async function refresh(silent = false) {
  const button = byId("refreshButton");
  button.disabled = true;
  try {
    const [dashboardDocument, jobsDocument] = await Promise.all([
      api("/api/dashboard"),
      api("/api/jobs"),
    ]);
    dashboard = dashboardDocument;
    setSourceJobs(jobsDocument.jobs || []);
    setGlossaryJobs(jobsDocument.glossary_jobs || []);
    setTranslationJobs(jobsDocument.translation_jobs || []);
    render();
    scheduleSourceJobPoll();
    const time = new Intl.DateTimeFormat("ko-KR", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date());
    byId("lastUpdated").textContent = `${time} 기준`;
    if (!silent) showToast("프로젝트 상태를 새로 불러왔습니다.");
  } catch (error) {
    byId("lastUpdated").textContent = "연결을 확인해 주세요";
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function openReview(projectId, reviewType) {
  try {
    const result = await api("/api/review/open", {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        review_type: reviewType,
      }),
    });
    window.location.assign(result.url);
  } catch (error) {
    showToast(error.message, true);
    await refresh(true);
  }
}

async function saveDownload(
  button,
  {
    requestUrl,
    downloadName,
    description,
    mimeType,
    extensions,
  },
) {
  button.disabled = true;
  try {
    let fileHandle = null;
    if (typeof window.showSaveFilePicker === "function") {
      try {
        fileHandle = await window.showSaveFilePicker({
          suggestedName: downloadName,
          types: [{
            description,
            accept: {[mimeType]: extensions},
          }],
        });
      } catch (error) {
        if (error?.name === "AbortError") return;
        throw error;
      }
    }

    const response = await fetch(requestUrl, {
      headers: {"X-GLK-Token": AUTH_TOKEN},
    });
    if (!response.ok) {
      const payload = await response.json().catch(
        () => ({message: `HTTP ${response.status}`}),
      );
      throw new Error(payload.message || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    if (fileHandle) {
      const writable = await fileHandle.createWritable();
      try {
        await writable.write(blob);
        await writable.close();
      } catch (error) {
        await writable.abort().catch(() => {});
        throw error;
      }
      showToast(`'${downloadName}' 파일을 저장했습니다.`);
    } else {
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = downloadName;
      anchor.hidden = true;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      showToast(`'${downloadName}' 다운로드를 시작했습니다.`);
    }
  } catch (error) {
    showToast(error.message, true);
    await refresh(true);
  } finally {
    button.disabled = false;
  }
}

async function downloadOutput(button) {
  const query = new URLSearchParams({
    project_id: button.dataset.project,
    path: button.dataset.downloadOutput,
  });
  await saveDownload(button, {
    requestUrl: `/api/output?${query}`,
    downloadName:
      button.dataset.downloadName || "translation.txt",
    description: "텍스트 파일",
    mimeType: "text/plain",
    extensions: [".txt"],
  });
}

async function downloadSourceOutput(button) {
  const query = new URLSearchParams({
    project_id: button.dataset.project,
  });
  await saveDownload(button, {
    requestUrl: `/api/source-output?${query}`,
    downloadName:
      button.dataset.downloadName || "approved_source.txt",
    description: "텍스트 파일",
    mimeType: "text/plain",
    extensions: [".txt"],
  });
}

async function downloadOutputArchive(button) {
  const projectId = button.dataset.project;
  const query = new URLSearchParams({project_id: projectId});
  await saveDownload(button, {
    requestUrl: `/api/output-archive?${query}`,
    downloadName:
      button.dataset.downloadName
      || `${projectId}_image_outputs.zip`,
    description: "ZIP 압축 파일",
    mimeType: "application/zip",
    extensions: [".zip"],
  });
}

function openProjectDialog() {
  byId("projectDialog").showModal();
  byId("projectName").focus();
}

function closeProjectDialog() {
  byId("projectDialog").close();
}

function selectedSourceType() {
  return byId("sourceForm").elements.source_type.value;
}

function promptBytes(inputId) {
  return new TextEncoder().encode(byId(inputId).value).length;
}

function updatePromptCount(inputId, countId) {
  const bytes = promptBytes(inputId);
  const count = byId(countId);
  count.textContent =
    `${bytes.toLocaleString("ko-KR")} / ${MAX_OCR_PROMPT_BYTES.toLocaleString("ko-KR")} bytes`;
  count.classList.toggle("limit", bytes > MAX_OCR_PROMPT_BYTES);
  return bytes;
}

function updateOcrPromptEditCount() {
  updatePromptCount("ocrPromptEdit", "ocrPromptEditCount");
}

function updateSourceFileInput() {
  const input = byId("sourceFiles");
  const images = selectedSourceType() === "images";
  input.value = "";
  input.multiple = images;
  input.accept = images
    ? ".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"
    : ".pdf,application/pdf";
  byId("sourceFilesLabel").textContent =
    images ? "이미지 파일" : "PDF 파일";
  byId("sourceFilesHelp").textContent = images
    ? "PNG, JPG, JPEG, WebP 파일을 최대 200개, 합계 500 MiB까지 선택하세요. 파일명 자연순으로 등록합니다."
    : "PDF 파일 한 개를 선택하세요. 선택 파일 한도는 500 MiB입니다.";
  byId("selectedFiles").replaceChildren();
}

function renderSelectedFiles() {
  const collator = new Intl.Collator("ko", {
    numeric: true,
    sensitivity: "base",
  });
  const files = [...byId("sourceFiles").files]
    .sort((left, right) => collator.compare(left.name, right.name));
  const list = byId("selectedFiles");
  list.replaceChildren();
  for (const file of files) {
    const item = document.createElement("li");
    item.textContent = file.name;
    list.append(item);
  }
}

function openSourceDialog(projectId, projectName, replace, sourceType) {
  const form = byId("sourceForm");
  form.reset();
  if (replace && ["pdf", "images"].includes(sourceType)) {
    form.elements.source_type.value = sourceType;
  }
  pendingSourceProject = {
    projectId,
    projectName,
    replace,
  };
  byId("sourceDialogKicker").textContent =
    replace ? "Replace originals" : "Register originals";
  byId("sourceDialogTitle").textContent =
    replace ? "원본 교체" : "원본 등록";
  byId("sourceDialogNote").innerHTML = replace
    ? "기존 원본은 삭제되고 선택한 파일로 교체됩니다. 저장된 OCR 프롬프트는 유지되며 별도의 <code>OCR 프롬프트 수정</code> 버튼에서 변경할 수 있습니다. 원문 추출·OCR 시작 전까지만 교체할 수 있습니다."
    : "선택한 파일은 프로젝트의 <code>01_input</code>에 복사합니다. 이미지 형식은 프로젝트 기본 OCR 프롬프트를 유지하며, 등록 뒤 별도의 <code>OCR 프롬프트 수정</code> 버튼에서 변경할 수 있습니다. 이 단계에서는 AI API 호출을 실행하지 않습니다.";
  byId("sourceSubmit").textContent =
    replace ? "기존 원본 교체" : "원본 등록";
  byId("sourceSubmit").classList.toggle(
    "source-replace-submit",
    replace,
  );
  byId("sourceProjectName").textContent = projectName;
  byId("sourceProjectId").textContent = projectId;
  updateSourceFileInput();
  byId("sourceDialog").showModal();
  byId("sourceFiles").focus();
}

function closeSourceDialog() {
  byId("sourceDialog").close();
}

function openOcrPromptDialog(projectId) {
  const project = dashboard?.projects.find(
    (value) => value.project_id === projectId
  );
  if (!project?.ocr_prompt_edit?.allowed) {
    showToast(
      project?.ocr_prompt_edit?.reason
        || "OCR 프롬프트를 수정할 수 없습니다.",
      true,
    );
    return;
  }
  pendingOcrPromptProject = {
    projectId,
    savedOcrPrompt: project.ocr_prompt || "",
  };
  byId("ocrPromptProjectName").textContent = project.name;
  byId("ocrPromptProjectId").textContent = project.project_id;
  byId("ocrPromptEdit").value = project.ocr_prompt || "";
  updateOcrPromptEditCount();
  byId("ocrPromptDialog").showModal();
  byId("ocrPromptEdit").focus();
}

function closeOcrPromptDialog() {
  byId("ocrPromptDialog").close();
}

function updateTranslationPromptEditCount() {
  const value = byId("translationPromptEdit").value;
  const byteCount = new TextEncoder().encode(value).length;
  const overLimit = byteCount > MAX_TRANSLATION_PROMPT_BYTES;
  const count = byId("translationPromptEditCount");
  count.textContent =
    `${byteCount.toLocaleString()} / ${MAX_TRANSLATION_PROMPT_BYTES.toLocaleString()} bytes`;
  count.classList.toggle("limit", overLimit);
  byId("translationPromptSave").disabled =
    overLimit || !value.trim();
}

function openTranslationPromptDialog(projectId) {
  const project = dashboard?.projects.find(
    (value) => value.project_id === projectId,
  );
  if (!project) {
    showToast("프로젝트 정보를 찾지 못했습니다.", true);
    return;
  }
  if (activeBackgroundJob()) {
    showToast(
      "백그라운드 작업 중에는 번역 프롬프트를 수정할 수 없습니다.",
      true,
    );
    return;
  }
  const savedPrompt = project.translation_prompt?.value || "";
  pendingTranslationPromptProject = {
    projectId,
    savedPrompt,
    expectedSha256: project.translation_prompt?.sha256 || "",
  };
  byId("translationPromptProjectName").textContent = project.name;
  byId("translationPromptProjectId").textContent = project.project_id;
  byId("translationPromptEdit").value = savedPrompt;
  const status = project.pipeline.translation_status;
  const impact = byId("translationPromptImpact");
  impact.hidden = !["partial", "current", "stale"].includes(status);
  impact.textContent = status === "partial"
    ? "프롬프트를 변경하면 저장된 일부 청크를 이어갈 수 없으며 전체 재번역이 필요합니다."
    : "프롬프트를 변경하면 현재 번역 승인이 해제되고 전체 재번역이 필요합니다. 기존 파일은 revisions에 보관됩니다.";
  updateTranslationPromptEditCount();
  byId("translationPromptDialog").showModal();
  byId("translationPromptEdit").focus();
}

function closeTranslationPromptDialog() {
  byId("translationPromptDialog").close();
}

async function saveTranslationPrompt(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!pendingTranslationPromptProject || !form.reportValidity()) return;
  const prompt = byId("translationPromptEdit").value;
  if (!prompt.trim()) {
    showToast("번역 프롬프트를 입력하세요.", true);
    byId("translationPromptEdit").focus();
    return;
  }
  if (
    new TextEncoder().encode(prompt).length
    > MAX_TRANSLATION_PROMPT_BYTES
  ) {
    showToast("번역 프롬프트는 64 KiB 이하여야 합니다.", true);
    byId("translationPromptEdit").focus();
    return;
  }
  const submit = byId("translationPromptSave");
  submit.disabled = true;
  try {
    const response = await api(
      `/api/projects/${encodeURIComponent(
        pendingTranslationPromptProject.projectId
      )}/translation-prompt`,
      {
        method: "PATCH",
        body: JSON.stringify({
          translation_prompt: prompt,
          expected_sha256:
            pendingTranslationPromptProject.expectedSha256,
        }),
      },
    );
    const invalidated =
      response.translation_prompt?.translation_invalidated;
    closeTranslationPromptDialog();
    await refresh(true);
    showToast(
      invalidated
        ? "번역 프롬프트를 저장했습니다. 전체 재번역이 필요합니다."
        : "번역 프롬프트를 저장했습니다.",
    );
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
  }
}

function openSourceFilesDialog(projectId) {
  const project = dashboard?.projects.find(
    (value) => value.project_id === projectId
  );
  if (!project) {
    showToast("프로젝트 파일 정보를 찾지 못했습니다.", true);
    return;
  }
  pendingSourceFilesProject = project;
  byId("sourceFilesProjectName").textContent = project.name;
  byId("sourceFilesProjectId").textContent =
    `${project.project_id} · ${project.source_files.length}개`;
  const list = byId("registeredSourceFiles");
  list.replaceChildren();
  for (const filename of project.source_files) {
    const item = document.createElement("li");
    item.textContent = filename;
    list.append(item);
  }
  const replaceButton = byId("sourceFilesReplace");
  replaceButton.hidden = !project.source_replacement?.allowed;
  replaceButton.title = project.source_replacement?.reason || "";
  byId("sourceFilesDialog").showModal();
  byId("sourceFilesDialogDone").focus();
}

function closeSourceFilesDialog() {
  byId("sourceFilesDialog").close();
}

function replaceFromSourceFiles() {
  const project = pendingSourceFilesProject;
  if (!project?.source_replacement?.allowed) return;
  closeSourceFilesDialog();
  openSourceDialog(
    project.project_id,
    project.name,
    true,
    project.source_type,
  );
}

function openDeleteDialog(projectId, projectName) {
  pendingDeleteProject = {projectId, projectName};
  byId("deleteProjectName").textContent = projectName;
  byId("deleteProjectId").textContent = projectId;
  byId("deleteDialog").showModal();
  byId("deleteDialogCancel").focus();
}

function closeDeleteDialog() {
  byId("deleteDialog").close();
}

async function createProject(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const submit = byId("projectSubmit");
  submit.disabled = true;
  try {
    const result = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        name: byId("projectName").value,
        project_id: byId("projectId").value || null,
      }),
    });
    closeProjectDialog();
    form.reset();
    activeFilter = "all";
    byId("searchInput").value = "";
    document.querySelectorAll(".filter").forEach((button) => {
      button.classList.toggle("active", button.dataset.filter === "all");
    });
    await refresh(true);
    showToast(`'${result.project.name}' 프로젝트를 생성했습니다.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
  }
}

async function deleteProject(event) {
  event.preventDefault();
  if (!pendingDeleteProject) return;
  const project = pendingDeleteProject;
  const submit = byId("deleteSubmit");
  submit.disabled = true;
  try {
    await api(`/api/projects/${encodeURIComponent(project.projectId)}`, {
      method: "DELETE",
    });
    window.location.reload();
  } catch (error) {
    showToast(error.message, true);
    submit.disabled = false;
  }
}

async function registerSource(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!pendingSourceProject || !form.reportValidity()) return;
  const submit = byId("sourceSubmit");
  const sourceType = selectedSourceType();
  const collator = new Intl.Collator("ko", {
    numeric: true,
    sensitivity: "base",
  });
  const files = [...byId("sourceFiles").files]
    .sort((left, right) => collator.compare(left.name, right.name));
  if (
    (sourceType === "pdf" && files.length !== 1)
    || (sourceType === "images" && !files.length)
  ) {
    showToast("등록할 원본 파일을 확인하세요.", true);
    return;
  }
  if (files.length > 200) {
    showToast("이미지는 한 번에 최대 200개까지 등록할 수 있습니다.", true);
    return;
  }
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  if (totalBytes > MAX_SOURCE_UPLOAD_BYTES) {
    showToast("전체 파일 크기는 500 MiB 이하여야 합니다.", true);
    return;
  }

  const body = new FormData();
  body.append("source_type", sourceType);
  for (const file of files) body.append("files", file, file.name);
  submit.disabled = true;
  try {
    const result = await api(
      `/api/projects/${encodeURIComponent(
        pendingSourceProject.projectId
      )}/source`,
      {
        method: pendingSourceProject.replace ? "PUT" : "POST",
        body,
      },
    );
    const replaced = pendingSourceProject.replace;
    closeSourceDialog();
    await refresh(true);
    const count = result.source.files.length;
    showToast(
      `${sourceType === "pdf" ? "PDF" : `이미지 ${count}개`} 원본을 ${replaced ? "교체" : "등록"}했습니다.`
    );
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
  }
}

async function saveOcrPrompt(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!pendingOcrPromptProject || !form.reportValidity()) return;
  const prompt = byId("ocrPromptEdit").value;
  if (!prompt.trim()) {
    showToast("이미지 OCR 프롬프트를 입력하세요.", true);
    byId("ocrPromptEdit").focus();
    return;
  }
  if (promptBytes("ocrPromptEdit") > MAX_OCR_PROMPT_BYTES) {
    showToast("OCR 프롬프트는 64 KiB 이하여야 합니다.", true);
    byId("ocrPromptEdit").focus();
    return;
  }
  const submit = byId("ocrPromptSubmit");
  submit.disabled = true;
  try {
    await api(
      `/api/projects/${encodeURIComponent(
        pendingOcrPromptProject.projectId
      )}/ocr-prompt`,
      {
        method: "PATCH",
        body: JSON.stringify({ocr_prompt: prompt}),
      },
    );
    closeOcrPromptDialog();
    await refresh(true);
    showToast("이미지 OCR 프롬프트를 저장했습니다.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submit.disabled = false;
  }
}

byId("refreshButton").addEventListener("click", () => refresh(false));
byId("aiSettingsButton").addEventListener(
  "click",
  openAiSettingsDialog,
);
byId("aiSettingsDialogClose").addEventListener(
  "click",
  closeAiSettingsDialog,
);
byId("aiSettingsDialogCancel").addEventListener(
  "click",
  closeAiSettingsDialog,
);
byId("aiSettingsForm").addEventListener("submit", saveAiSettings);
byId("aiProvider").addEventListener("change", () => {
  updateAiProviderInput();
  byId("aiApiKey").value = "";
});
byId("aiModelPreset").addEventListener("change", updateAiModelInput);
byId("aiApiKeyToggle").addEventListener("click", () => {
  const input = byId("aiApiKey");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  byId("aiApiKeyToggle").textContent = showing ? "보기" : "숨기기";
  input.focus();
});
byId("aiSettingsDialog").addEventListener("close", () => {
  byId("aiSettingsForm").reset();
  byId("aiApiKey").type = "password";
  byId("aiApiKeyToggle").textContent = "보기";
  byId("aiSettingsSubmit").disabled = false;
  updateAiModelInput();
});
byId("sourceJobDialogClose").addEventListener(
  "click",
  closeSourceJobDialog,
);
byId("sourceJobDialogCancel").addEventListener(
  "click",
  closeSourceJobDialog,
);
byId("sourceJobForm").addEventListener("submit", startSourceJob);
byId("sourceJobDialog").addEventListener("close", () => {
  pendingSourceJobProject = null;
  byId("sourceJobOcrPromptField").hidden = true;
  byId("sourceJobOcrPrompt").value = "";
  byId("sourceJobSubmit").disabled = false;
});
byId("glossaryJobDialogClose").addEventListener(
  "click",
  closeGlossaryJobDialog,
);
byId("glossaryJobDialogCancel").addEventListener(
  "click",
  closeGlossaryJobDialog,
);
byId("glossaryJobForm").addEventListener("submit", startGlossaryJob);
byId("glossaryJobDialog").addEventListener("close", () => {
  pendingGlossaryJobProject = null;
  byId("glossaryJobSubmit").disabled = false;
});
byId("translationJobDialogClose").addEventListener(
  "click",
  closeTranslationJobDialog,
);
byId("translationJobDialogCancel").addEventListener(
  "click",
  closeTranslationJobDialog,
);
byId("translationJobForm").addEventListener(
  "submit",
  startTranslationJob,
);
byId("translationPrompt").addEventListener(
  "input",
  updateTranslationPromptCount,
);
byId("translationJobDialog").addEventListener("close", () => {
  pendingTranslationJobProject = null;
  pendingTranslationForce = false;
  byId("translationJobForm").reset();
  byId("translationPrompt").readOnly = false;
  byId("translationResumeNote").hidden = true;
  byId("translationForceNote").hidden = true;
  byId("translationJobSubmit").disabled = false;
  updateTranslationPromptCount();
});
byId("emptyCreateButton").addEventListener("click", openProjectDialog);
byId("projectDialogClose").addEventListener("click", closeProjectDialog);
byId("projectDialogCancel").addEventListener("click", closeProjectDialog);
byId("projectForm").addEventListener("submit", createProject);
byId("projectDialog").addEventListener("close", () => {
  byId("projectForm").reset();
});
byId("sourceDialogClose").addEventListener("click", closeSourceDialog);
byId("sourceDialogCancel").addEventListener("click", closeSourceDialog);
byId("sourceForm").addEventListener("submit", registerSource);
byId("sourceForm").addEventListener("change", (event) => {
  if (event.target.name === "source_type") updateSourceFileInput();
  if (event.target.id === "sourceFiles") renderSelectedFiles();
});
byId("sourceDialog").addEventListener("close", () => {
  pendingSourceProject = null;
  byId("sourceForm").reset();
  byId("sourceSubmit").disabled = false;
  updateSourceFileInput();
});
byId("ocrPromptDialogClose").addEventListener(
  "click",
  closeOcrPromptDialog,
);
byId("ocrPromptDialogCancel").addEventListener(
  "click",
  closeOcrPromptDialog,
);
byId("ocrPromptForm").addEventListener("submit", saveOcrPrompt);
byId("ocrPromptEdit").addEventListener(
  "input",
  updateOcrPromptEditCount,
);
byId("ocrPromptEditReset").addEventListener("click", () => {
  byId("ocrPromptEdit").value =
    pendingOcrPromptProject?.savedOcrPrompt || "";
  updateOcrPromptEditCount();
  byId("ocrPromptEdit").focus();
});
byId("ocrPromptDialog").addEventListener("close", () => {
  pendingOcrPromptProject = null;
  byId("ocrPromptForm").reset();
  byId("ocrPromptSubmit").disabled = false;
  updateOcrPromptEditCount();
});
byId("translationPromptDialogClose").addEventListener(
  "click",
  closeTranslationPromptDialog,
);
byId("translationPromptDialogCancel").addEventListener(
  "click",
  closeTranslationPromptDialog,
);
byId("translationPromptForm").addEventListener(
  "submit",
  saveTranslationPrompt,
);
byId("translationPromptEdit").addEventListener(
  "input",
  updateTranslationPromptEditCount,
);
byId("translationPromptEditReset").addEventListener("click", () => {
  byId("translationPromptEdit").value =
    pendingTranslationPromptProject?.savedPrompt || "";
  updateTranslationPromptEditCount();
  byId("translationPromptEdit").focus();
});
byId("translationPromptDialog").addEventListener("close", () => {
  pendingTranslationPromptProject = null;
  byId("translationPromptForm").reset();
  byId("translationPromptImpact").hidden = true;
  byId("translationPromptSave").disabled = false;
  updateTranslationPromptEditCount();
});
byId("sourceFilesDialogClose").addEventListener(
  "click",
  closeSourceFilesDialog,
);
byId("sourceFilesDialogDone").addEventListener(
  "click",
  closeSourceFilesDialog,
);
byId("sourceFilesReplace").addEventListener(
  "click",
  replaceFromSourceFiles,
);
byId("sourceFilesDialog").addEventListener("close", () => {
  pendingSourceFilesProject = null;
  byId("registeredSourceFiles").replaceChildren();
});
byId("deleteDialogClose").addEventListener("click", closeDeleteDialog);
byId("deleteDialogCancel").addEventListener("click", closeDeleteDialog);
byId("deleteForm").addEventListener("submit", deleteProject);
byId("deleteDialog").addEventListener("close", () => {
  pendingDeleteProject = null;
  byId("deleteSubmit").disabled = false;
});
byId("searchInput").addEventListener("input", render);
document.querySelector(".filters").addEventListener("click", (event) => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  activeFilter = button.dataset.filter;
  document.querySelectorAll(".filter").forEach((value) => {
    value.classList.toggle("active", value === button);
  });
  render();
});
byId("projectGrid").addEventListener("click", (event) => {
  const sourceDownload = event.target.closest(
    "[data-download-source]",
  );
  if (sourceDownload) {
    downloadSourceOutput(sourceDownload);
    return;
  }
  const archiveButton = event.target.closest(
    "[data-download-output-archive]",
  );
  if (archiveButton) {
    downloadOutputArchive(archiveButton);
    return;
  }
  const downloadButton = event.target.closest("[data-download-output]");
  if (downloadButton) {
    downloadOutput(downloadButton);
    return;
  }
  const settingsButton = event.target.closest(
    "[data-require-ai-settings]",
  );
  if (settingsButton) {
    openAiSettingsDialog();
    return;
  }
  const jobButton = event.target.closest("[data-start-source-job]");
  if (jobButton) {
    openSourceJobDialog(jobButton.dataset.startSourceJob);
    return;
  }
  const glossaryStartButton = event.target.closest(
    "[data-start-glossary-job]",
  );
  if (glossaryStartButton) {
    openGlossaryJobDialog(
      glossaryStartButton.dataset.startGlossaryJob,
    );
    return;
  }
  const translationStartButton = event.target.closest(
    "[data-start-translation-job]",
  );
  if (translationStartButton) {
    openTranslationJobDialog(
      translationStartButton.dataset.startTranslationJob,
    );
    return;
  }
  const translationPromptButton = event.target.closest(
    "[data-edit-translation-prompt]",
  );
  if (translationPromptButton) {
    openTranslationPromptDialog(
      translationPromptButton.dataset.editTranslationPrompt,
    );
    return;
  }
  const promptButton = event.target.closest("[data-edit-ocr-prompt]");
  if (promptButton) {
    openOcrPromptDialog(promptButton.dataset.editOcrPrompt);
    return;
  }
  const sourceFilesButton = event.target.closest("[data-source-files]");
  if (sourceFilesButton) {
    openSourceFilesDialog(sourceFilesButton.dataset.sourceFiles);
    return;
  }
  const sourceButton = event.target.closest("[data-register-source]");
  if (sourceButton) {
    openSourceDialog(
      sourceButton.dataset.registerSource,
      sourceButton.dataset.projectName,
      sourceButton.dataset.replaceSource === "true",
      sourceButton.dataset.sourceType || null,
    );
    return;
  }
  const deleteButton = event.target.closest("[data-delete-project]");
  if (deleteButton) {
    openDeleteDialog(
      deleteButton.dataset.deleteProject,
      deleteButton.dataset.projectName,
    );
    return;
  }
  const createButton = event.target.closest("[data-create-project]");
  if (createButton) {
    openProjectDialog();
    return;
  }
  const button = event.target.closest("[data-review]");
  if (!button || button.disabled) return;
  openReview(button.dataset.project, button.dataset.review);
});

refresh(true);
loadAiSettings(true).catch(() => {});
