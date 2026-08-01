(function () {
  "use strict";

  const registry = window.__HERMES_PLUGINS__;
  if (!registry) return;

  registry.register("record", () => null);
})();
