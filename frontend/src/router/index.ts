import { createRouter, createWebHistory } from "vue-router";

import BatchJobView from "../views/BatchJobView.vue";
import JobListView from "../views/JobListView.vue";
import PDFBookOCRView from "../views/PDFBookOCRView.vue";
import ProofreadingMemoryView from "../views/ProofreadingMemoryView.vue";
import SettingsView from "../views/SettingsView.vue";
import SingleJobView from "../views/SingleJobView.vue";
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
      path: "/pdf-book-ocr",
      name: "pdf-book-ocr",
      component: PDFBookOCRView,
    },
    {
      path: "/proofreading-memory",
      name: "proofreading-memory",
      component: ProofreadingMemoryView,
    },
    {
      path: "/jobs",
      name: "jobs",
      component: JobListView,
    },
    {
      path: "/settings",
      name: "settings",
      component: SettingsView,
    },
  ],
});
