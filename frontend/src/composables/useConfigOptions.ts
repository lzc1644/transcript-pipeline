import { onMounted, ref } from "vue";

import { getConfig, getFrontendSettings, type AsrCandidate } from "../api/client";

export function useConfigOptions() {
  const profiles = ref<string[]>([]);
  const asrCandidates = ref<AsrCandidate[]>([]);
  const defaultAsrCandidate = ref("");
  const defaultSecondaryAsrCandidate = ref("");
  const videoExtensions = ref<string[]>([]);
  const referenceExtensions = ref<string[]>([]);
  const activeProfile = ref("");
  const defaultOcrModel = ref("");
  const defaultOcrReasoningEffort = ref("");
  const defaultFastMode = ref<boolean | null>(null);
  const defaultOcrFastMode = ref<boolean | null>(null);
  const defaultOcrMaxConcurrency = ref(40);
  const defaultOcrSubmitIntervalSeconds = ref(5);
  const defaultOutputDir = ref("");
  const uploadDir = ref("");
  const loading = ref(false);
  const error = ref("");

  async function load() {
    loading.value = true;
    error.value = "";
    try {
      const config = await getConfig();
      profiles.value = config.profiles;
      asrCandidates.value = config.asr_candidates ?? [];
      videoExtensions.value = config.video_extensions;
      referenceExtensions.value = config.reference_extensions;
      activeProfile.value = config.active_profile;
      defaultOutputDir.value = config.default_output_dir;
      uploadDir.value = config.upload_dir;

      const settings = await getFrontendSettings();
      defaultAsrCandidate.value = settings.asr_candidate || "whisper-existing";
      defaultSecondaryAsrCandidate.value = settings.secondary_asr_candidate || "";
      defaultOcrModel.value = settings.ocr_model;
      defaultOcrReasoningEffort.value = settings.ocr_reasoning_effort;
      defaultFastMode.value = settings.fast_mode ?? false;
      defaultOcrFastMode.value = settings.ocr_fast_mode ?? false;
      defaultOcrMaxConcurrency.value = settings.ocr_max_concurrency;
      defaultOcrSubmitIntervalSeconds.value = settings.ocr_submit_interval_seconds;
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : "加载配置失败";
    } finally {
      loading.value = false;
    }
  }

  onMounted(load);

  return {
    asrCandidates,
    defaultAsrCandidate,
    defaultSecondaryAsrCandidate,
    profiles,
    videoExtensions,
    referenceExtensions,
    activeProfile,
    defaultOcrModel,
    defaultOcrReasoningEffort,
    defaultFastMode,
    defaultOcrFastMode,
    defaultOcrMaxConcurrency,
    defaultOcrSubmitIntervalSeconds,
    defaultOutputDir,
    uploadDir,
    loading,
    error,
    reload: load,
  };
}
