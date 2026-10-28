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


  const presenceRoot = document.querySelector('[data-whatsapp-presence]');
  if (presenceRoot) {
    const csrf = presenceRoot.dataset.csrf || '';
    const role = presenceRoot.dataset.role || '';
    const toggle = presenceRoot.querySelector('[data-whatsapp-toggle]');
    const dot = presenceRoot.querySelector('[data-whatsapp-dot]');
    const label = presenceRoot.querySelector('[data-whatsapp-label]');
    const detail = presenceRoot.querySelector('[data-whatsapp-detail]');
    let online = false;
    let heartbeatTimer = null;

    const renderPresence = (state) => {
      online = Boolean(state?.online);
      presenceRoot.classList.toggle('online', online);
      presenceRoot.classList.toggle('offline', !online);
      if (dot) dot.title = online ? 'Online no WhatsApp' : 'Offline no WhatsApp';
      if (label) label.textContent = online ? 'WhatsApp online' : 'WhatsApp offline';
      if (detail) {
        const group = state?.team || 'Equipe';
        const onlineCount = Number(state?.group_online_count || 0);
        detail.textContent = online
          ? group + ' · ' + onlineCount + ' online no grupo'
          : group + ' · fora da distribuição';
      }
      if (toggle) {
        toggle.textContent = online ? 'Sair do WhatsApp' : 'Entrar no WhatsApp';
        toggle.classList.toggle('danger', online);
      }
      if (!online && role === 'CONSULTOR' && window.location.pathname.startsWith('/conversations')) {
        window.location.replace('/dashboard');
      }
    };

    const postPresence = async (action) => {
      const form = new FormData();
      form.append('csrf_token', csrf);
      const response = await fetch('/api/whatsapp-presence/' + action, {
        method: 'POST',
        body: form,
        credentials: 'same-origin',
        headers: {'Accept': 'application/json'}
      });
      if (response.status === 401) {
        window.location.href = '/login';
        return null;
      }
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.detail || payload?.error || 'Não foi possível atualizar sua presença no WhatsApp.');
      }
      renderPresence(payload);
      return payload;
    };

    const refreshPresence = async () => {
      try {
        const response = await fetch('/api/whatsapp-presence', {
          credentials: 'same-origin',
          headers: {'Accept': 'application/json'}
        });
        if (response.status === 401) return;
        if (!response.ok) return;
        renderPresence(await response.json());
      } catch (_) {}
    };

    const heartbeat = async () => {
      if (!online) return;
      try {
        await postPresence('heartbeat');
      } catch (_) {}
    };

    const ensureHeartbeat = () => {
      if (heartbeatTimer) window.clearInterval(heartbeatTimer);
      heartbeatTimer = window.setInterval(heartbeat, 20000);
    };

    toggle?.addEventListener('click', async () => {
      if (toggle.disabled) return;
      toggle.disabled = true;
      const previous = toggle.textContent;
      toggle.textContent = online ? 'Saindo...' : 'Entrando...';
      try {
        const state = await postPresence(online ? 'leave' : 'join');
        if (state?.online && window.location.pathname === '/dashboard') {
          // Mantém o usuário na página atual; Conversas já está liberado no menu.
        }
      } catch (error) {
        window.alert(error?.message || 'Não foi possível atualizar o WhatsApp.');
        toggle.textContent = previous;
      } finally {
        toggle.disabled = false;
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        refreshPresence();
        heartbeat();
      }
    });

    window.addEventListener('online', () => {
      refreshPresence();
      heartbeat();
    });

    refreshPresence();
    ensureHeartbeat();
  }

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
