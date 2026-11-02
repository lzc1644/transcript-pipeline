<template>
  <div class="pdf-workspace">
    <nav class="pdf-tabs" aria-label="PDF 处理">
      <router-link
        to="/pdf/library"
        class="pdf-tab-link"
        active-class="is-active"
      >
        书库
      </router-link>
      <router-link
        to="/pdf/ocr"
        class="pdf-tab-link"
        active-class="is-active"
      >
        整本 OCR
      </router-link>
    </nav>
    <router-view />
  </div>
</template>

<style scoped>
.pdf-workspace {
  display: grid;
  gap: 24px;
  min-width: 0;
}

.pdf-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  border-bottom: 1px solid var(--border-subtle);
}

.pdf-tab-link {
  display: inline-flex;
  align-items: center;
  padding: 10px 18px;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  color: var(--text-secondary);
  text-decoration: none;
  font-weight: 500;
  border-radius: 6px 6px 0 0;
  transition: color var(--motion-fast) var(--ease-out),
              background-color var(--motion-fast) var(--ease-out),
              border-color var(--motion-fast) var(--ease-out);
}

.pdf-tab-link:hover {
  color: var(--primary);
  background-color: var(--surface-subtle);
}

.pdf-tab-link.is-active {
  border-bottom-color: var(--primary);
  color: var(--primary);
  font-weight: 600;
}

.pdf-tab-link:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: -2px;
}
</style>
