(() => {
  const sidebar = document.querySelector('#sidebar');
  const sidebarToggle = document.querySelector('[data-sidebar-toggle]');
  if (sidebar && sidebarToggle) {
    sidebarToggle.addEventListener('click', () => sidebar.classList.toggle('open'));
    document.addEventListener('click', (event) => {
      if (window.innerWidth <= 820 && sidebar.classList.contains('open') &&
          !sidebar.contains(event.target) && !sidebarToggle.contains(event.target)) {
        sidebar.classList.remove('open');
      }
    });
  }

  document.querySelectorAll('[data-dismiss-flash]').forEach((button) => {
    button.addEventListener('click', () => button.closest('.flash')?.remove());
  });
  window.setTimeout(() => {
    document.querySelectorAll('.flash').forEach((node) => node.remove());
  }, 5500);

  document.querySelectorAll('[data-modal-open]').forEach((button) => {
    button.addEventListener('click', () => {
      const modal = document.getElementById(button.dataset.modalOpen);
      if (modal) {
        modal.classList.add('open');
        modal.setAttribute('aria-hidden', 'false');
      }
    });
  });
  document.querySelectorAll('[data-modal-close]').forEach((button) => {
    button.addEventListener('click', () => {
      const modal = button.closest('.modal-backdrop');
      modal?.classList.remove('open');
      modal?.setAttribute('aria-hidden', 'true');
    });
  });
  document.querySelectorAll('.modal-backdrop').forEach((modal) => {
    modal.addEventListener('click', (event) => {
      if (event.target === modal) {
        modal.classList.remove('open');
        modal.setAttribute('aria-hidden', 'true');
      }
    });
  });

  document.querySelectorAll('.clickable-row').forEach((row) => {
    row.addEventListener('click', (event) => {
      if (event.target.closest('a, button, input, select')) return;
      const href = row.dataset.href;
      if (href) window.location.href = href;
    });
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      document.querySelectorAll('.modal-backdrop.open').forEach((modal) => {
        modal.classList.remove('open');
        modal.setAttribute('aria-hidden', 'true');
      });
      sidebar?.classList.remove('open');
    }
  });
})();
