// Live feed server base URL. On GitHub Pages this is the Azure Container App; served by the server itself, use
// same-origin. ?live=<url> overrides either.
window.FAULTLINES_LIVE_URL = location.hostname.endsWith("github.io")
  ? "https://faultlines-live.happydesert-83063759.eastus2.azurecontainerapps.io"
  : "";
