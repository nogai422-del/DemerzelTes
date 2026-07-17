(function () {
  const body = document.body;
  const openButton = document.querySelector('[data-sidebar-open]');
  const closeTargets = document.querySelectorAll('[data-sidebar-close]');

  if (openButton) {
    openButton.addEventListener('click', () => body.classList.add('sidebar-open'));
  }
  closeTargets.forEach((target) => {
    target.addEventListener('click', () => body.classList.remove('sidebar-open'));
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') body.classList.remove('sidebar-open');
  });
  document.querySelectorAll('.sidebar .nav-link').forEach((link) => {
    link.addEventListener('click', () => body.classList.remove('sidebar-open'));
  });

  document.querySelectorAll('input[type="file"]').forEach((input) => {
    const update = () => {
      const wrapper = input.closest('.file-field');
      const label = wrapper ? wrapper.querySelector('.file-name') : null;
      if (label) label.textContent = input.files && input.files[0] ? input.files[0].name : 'Файл не выбран';
    };
    input.addEventListener('change', update);
  });

  let formIsSubmitting = false;
  document.querySelectorAll('form').forEach((form) => {
    form.dataset.dirty = '0';
    const markDirty = () => {
      if (!formIsSubmitting) form.dataset.dirty = '1';
    };
    form.addEventListener('input', markDirty);
    form.addEventListener('change', markDirty);

    form.addEventListener('submit', (event) => {
      if (event.defaultPrevented) return;
      formIsSubmitting = true;
      form.dataset.dirty = '0';
      const button = form.querySelector('button[type="submit"]');
      if (!button || button.dataset.noLoading === '1') return;
      button.classList.add('is-loading');
      button.disabled = true;
    });
  });

  window.addEventListener('beforeunload', (event) => {
    if (formIsSubmitting) return;
    const hasUnsavedChanges = Array.from(document.querySelectorAll('form'))
      .some((form) => form.dataset.dirty === '1');
    if (!hasUnsavedChanges) return;
    event.preventDefault();
    event.returnValue = '';
  });

  const appearanceForm = document.querySelector('[data-appearance-form]');
  if (appearanceForm) {
    const root = document.documentElement;
    const themeSelect = appearanceForm.querySelector('[data-ui-theme]');
    const accentInput = appearanceForm.querySelector('[data-ui-accent]');
    const colorOutput = appearanceForm.querySelector('[data-color-output]');
    const opacityRange = appearanceForm.querySelector('[data-ui-opacity-range]');
    const opacityValue = appearanceForm.querySelector('[data-ui-opacity-value]');
    const opacityOutput = appearanceForm.querySelector('[data-opacity-output]');

    const hexToRgb = (hex) => {
      const value = String(hex || '').replace('#', '');
      if (!/^[0-9a-fA-F]{6}$/.test(value)) return '124, 108, 255';
      return [0, 2, 4].map((index) => parseInt(value.slice(index, index + 2), 16)).join(', ');
    };

    const applyTheme = () => {
      if (themeSelect) root.dataset.theme = themeSelect.value;
    };
    const applyAccent = () => {
      if (!accentInput) return;
      root.style.setProperty('--accent', accentInput.value);
      root.style.setProperty('--accent-rgb', hexToRgb(accentInput.value));
      if (colorOutput) colorOutput.textContent = accentInput.value.toUpperCase();
    };
    const applyOpacity = () => {
      if (!opacityRange) return;
      const percent = Math.max(15, Math.min(100, Number(opacityRange.value) || 90));
      const decimal = (percent / 100).toFixed(2);
      root.style.setProperty('--button-opacity', decimal);
      if (opacityValue) opacityValue.value = decimal;
      if (opacityOutput) opacityOutput.textContent = `${percent}%`;
    };
    const applyStyle = (input) => {
      if (input && input.checked) root.dataset.uiStyle = input.value;
    };

    if (themeSelect) themeSelect.addEventListener('change', applyTheme);
    if (accentInput) accentInput.addEventListener('input', applyAccent);
    if (opacityRange) opacityRange.addEventListener('input', applyOpacity);
    appearanceForm.querySelectorAll('[data-ui-style]').forEach((input) => {
      input.addEventListener('change', () => applyStyle(input));
      applyStyle(input);
    });

    applyTheme();
    applyAccent();
    applyOpacity();
  }

  const url = new URL(window.location.href);
  ['saved', 'uploaded', 'deleted', 'csrf'].forEach((key) => {
    if (url.searchParams.has(key)) url.searchParams.delete(key);
  });
  if (url.toString() !== window.location.href) history.replaceState({}, '', url.toString());
})();
