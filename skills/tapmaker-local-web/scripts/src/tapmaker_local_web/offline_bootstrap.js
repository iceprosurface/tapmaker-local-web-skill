// Offline transport for URLs constructed inside WASM/Lua. Only the official
// asset origin is mapped; the server serves a finite verified snapshot, never
// proxies. CSP independently denies all other external destinations.
(() => {
  const official = '__OFFLINE_ORIGIN__';
  const prefix = '__OFFLINE_PREFIX__';
  function localURL(input) {
    const url = new URL(String(input), location.href);
    if (url.origin === official && url.pathname.startsWith('/src/')) {
      return location.origin + prefix + url.pathname + url.search;
    }
    return String(input);
  }
  const originalFetch = window.fetch;
  window.fetch = function(input, init) {
    if (input instanceof Request) {
      const url = localURL(input.url);
      if (url !== input.url) input = new Request(url, input);
    } else {
      input = localURL(input);
    }
    return originalFetch.call(this, input, init);
  };
  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    return originalOpen.call(this, method, localURL(url), ...rest);
  };
})();
