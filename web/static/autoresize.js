(function () {
  function adjustHeight(textarea) {
    if (!textarea || textarea.tagName !== 'TEXTAREA') return;
    textarea.style.height = 'auto';
    textarea.style.height = Math.max(textarea.scrollHeight, 88) + 'px';
  }

  function prepare(textarea) {
    if (!textarea || textarea.dataset.autoresizeReady === '1') return;
    textarea.dataset.autoresizeReady = '1';
    adjustHeight(textarea);
  }

  document.querySelectorAll('textarea').forEach(prepare);

  document.addEventListener('input', function (event) {
    if (event.target && event.target.tagName === 'TEXTAREA') {
      prepare(event.target);
      adjustHeight(event.target);
    }
  });

  document.addEventListener('toggle', function (event) {
    if (event.target && event.target.matches('details[open]')) {
      event.target.querySelectorAll('textarea').forEach(function (textarea) {
        prepare(textarea);
        requestAnimationFrame(function () { adjustHeight(textarea); });
      });
    }
  }, true);

  const observer = new MutationObserver(function (mutations) {
    mutations.forEach(function (mutation) {
      mutation.addedNodes.forEach(function (node) {
        if (!(node instanceof Element)) return;
        if (node.matches('textarea')) prepare(node);
        node.querySelectorAll('textarea').forEach(prepare);
      });
    });
  });
  observer.observe(document.body, { childList: true, subtree: true });
})();
