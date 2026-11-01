import { createRouter, createWebHistory } from "vue-router";

import DictationView from "../views/DictationView.vue";
import DictationExtensionView from "../views/DictationExtensionView.vue";
import BatchJobView from "../views/BatchJobView.vue";
import JobListView from "../views/JobListView.vue";
import PDFBookOCRView from "../views/PDFBookOCRView.vue";
import PDFWorkspaceView from "../views/PDFWorkspaceView.vue";
import SettingsView from "../views/SettingsView.vue";
import SingleJobView from "../views/SingleJobView.vue";
import LibraryView from "../views/LibraryView.vue";
import StageRunnerView from "../views/StageRunnerView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      redirect: "/single-job",
    },
    {
      path: "/single-job",
      name: "single-job",
      component: SingleJobView,
    },
    {
      path: "/batch-job",
      name: "batch-job",
      component: BatchJobView,
    },
    {
      path: "/stage-runner",
      name: "stage-runner",
      component: StageRunnerView,
    },
    {
      path: "/pdf",
      component: PDFWorkspaceView,
      children: [
        { path: "", redirect: { name: "library" } },
        { path: "library", name: "library", component: LibraryView },
        { path: "ocr", name: "pdf-book-ocr", component: PDFBookOCRView },
      ],
    },
    { path: "/library", redirect: to => ({ path: "/pdf/library", query: to.query, hash: to.hash }) },
    { path: "/pdf-book-ocr", redirect: to => ({ path: "/pdf/ocr", query: to.query, hash: to.hash }) },
    {
      path: "/jobs",
      name: "jobs",
      component: JobListView,
    },
    { path: "/dictation", name: "dictation", component: DictationView },
    { path: "/dictation/extension", name: "dictation-extension", component: DictationExtensionView },
    {
      path: "/settings",
      name: "settings",
      component: SettingsView,
    },
  ],
});
