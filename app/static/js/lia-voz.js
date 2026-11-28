// Read-only refresh on the test status page. Never replay the POST.
if (document.querySelector("[data-lia-active]")) {
  window.setTimeout(() => window.location.replace("/voice/lia-voz"), 3000);
}


// A Native techprefix is valid only for the Native 1804 account. Server
// authorization repeats this check; the browser never originates calls.
const liaAccount = document.querySelector('select[name="account_id"]');
const liaRoute = document.querySelector('select[name="route"]');
if (liaAccount && liaRoute) {
  const syncLiaRoutes = () => {
    const native = liaRoute.querySelector('option[value="native"]');
    if (!native) return;
    const selected = liaAccount.selectedOptions[0];
    native.disabled = !selected || selected.dataset.native !== 'sim';
    if (native.disabled && liaRoute.value === 'native') liaRoute.value = 'account';
  };
  liaAccount.addEventListener('change', syncLiaRoutes);
  syncLiaRoutes();
}
