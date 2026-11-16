/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_PRISM_ROOT?: string;
  /** @deprecated 迁移期兜底，改用 VITE_PRISM_ROOT。 */
  readonly VITE_AGENT_SENTINEL_ROOT?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
