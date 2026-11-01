<template>
  <div class="pdf-workspace">
    <nav class="pdf-tabs" aria-label="PDF 处理">
      <router-link to="/pdf/library" active-class="is-active">书库</router-link>
      <router-link to="/pdf/ocr" active-class="is-active">整本 OCR</router-link>
    </nav>
    <router-view />
  </div>
</template>

<style scoped>
.pdf-workspace { display: grid; gap: 24px; min-width: 0; }
.pdf-tabs { display: flex; flex-wrap: wrap; gap: 4px; border-bottom: 1px solid var(--border-subtle); }
.pdf-tabs a { display: inline-block; padding: 10px 16px; border-bottom: 2px solid transparent; color: var(--text-secondary); text-decoration: none; }
.pdf-tabs a:hover { color: var(--primary); }
.pdf-tabs a.is-active { border-bottom-color: var(--primary); color: var(--primary); font-weight: 600; }
.pdf-tabs a:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
</style>
