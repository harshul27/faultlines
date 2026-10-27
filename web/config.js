// Live feed server base URL. On a static host (GitHub Pages, Vercel) this is the Azure Container App; served by the
// server itself (Azure, or locally), use same-origin. ?live=<url> overrides either.
const sameOrigin = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname) || location.hostname.endsWith("azurecontainerapps.io");
window.FAULTLINES_LIVE_URL = sameOrigin ? "" : "https://faultlines-live2.salmoncoast-eea88059.eastus2.azurecontainerapps.io";
