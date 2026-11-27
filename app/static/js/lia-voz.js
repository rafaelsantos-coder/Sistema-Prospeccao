// Read-only refresh on the test status page. Never replay the POST.
if (document.querySelector("[data-lia-active]")) {
  window.setTimeout(() => window.location.replace("/voice/lia-voz"), 3000);
}
