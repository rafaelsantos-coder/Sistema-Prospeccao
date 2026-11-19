/* Permissões de usuários: interação de formulário; backend valida tudo novamente. */
(() => {
  "use strict";

  for (const matrix of document.querySelectorAll("[data-access-matrix]")) {
    const form = matrix.closest("form");
    const roleField = form?.querySelector("[data-access-role]");
    const choices = Array.from(matrix.querySelectorAll("[data-access-code]"));
    const byCode = new Map(choices.map(input => [input.dataset.accessCode, input]));
    const warning = matrix.querySelector("[data-access-warning]");
    const count = matrix.querySelector("[data-access-count]");
    const locked = matrix.dataset.accessLocked === "true";
    let defaults;
    try {
      defaults = JSON.parse(matrix.dataset.accessPresets || "{}");
    } catch (_error) {
      defaults = {};
    }

    const role = () => roleField?.value || form?.querySelector('input[name="role"]')?.value || "CONSULTOR";

    function isAssignable(input, selectedRole) {
      return !locked
        && selectedRole !== "ADMIN"
        && input.dataset.accessActor === "true"
        && (input.dataset.accessRoles || "").split(",").includes(selectedRole);
    }

    function updateCounts() {
      const selected = choices.filter(input => input.checked);
      if (count) count.textContent = selected.length + " permissão(ões) selecionada(s)";
      for (const group of matrix.querySelectorAll(".user-access-group")) {
        const countLabel = group.querySelector("[data-access-group-count]");
        if (!countLabel) continue;
        const groupChoices = Array.from(group.querySelectorAll("[data-access-code]"));
        countLabel.textContent = groupChoices.filter(input => input.checked).length +
          " / " + groupChoices.length;
      }
      for (const input of choices) {
        input.closest("label")?.classList.toggle("is-checked", input.checked);
        input.closest("label")?.classList.toggle("is-locked", input.disabled);
      }
    }

    function showWarning(text) {
      if (warning) warning.textContent = text || "";
    }

    function canCheckParent(input) {
      let parentCode = input.dataset.accessParent;
      while (parentCode) {
        const parent = byCode.get(parentCode);
        if (!parent || (parent.disabled && !parent.checked)) return false;
        parentCode = parent.dataset.accessParent;
      }
      return true;
    }

    function enforceDependencies() {
      for (const input of choices) {
        if (!input.checked) continue;
        const parent = byCode.get(input.dataset.accessParent);
        if (!parent) continue;
        if (parent.disabled && !parent.checked) {
          if (!input.disabled) input.checked = false;
          continue;
        }
        if (!parent.checked && !parent.disabled) parent.checked = true;
      }
    }

    function refreshRole(resetPreset) {
      const selectedRole = role();
      const preset = new Set(defaults[selectedRole] || []);
      for (const input of choices) {
        const assignable = isAssignable(input, selectedRole);
        input.disabled = !assignable;
        if (resetPreset) input.checked = selectedRole === "ADMIN" || (assignable && preset.has(input.value));
        else if (selectedRole === "ADMIN") input.checked = true;
        else if (!assignable && !(input.dataset.accessRoles || "").split(",").includes(selectedRole)) {
          input.checked = false;
        }
      }
      enforceDependencies();
      showWarning(selectedRole === "ADMIN"
        ? "Administradores têm acesso completo, sem restrições individuais."
        : "Ações dependem da permissão para visualizar o menu correspondente.");
      updateCounts();
    }

    for (const input of choices) {
      input.addEventListener("change", () => {
        if (input.checked) {
          if (!canCheckParent(input)) {
            input.checked = false;
            showWarning("Selecione primeiro o acesso ao menu principal.");
          } else {
            let parentCode = input.dataset.accessParent;
            while (parentCode) {
              const parent = byCode.get(parentCode);
              if (parent && !parent.disabled) parent.checked = true;
              parentCode = parent?.dataset.accessParent;
            }
            showWarning("");
          }
        } else {
          // Remover acesso ao menu revoga ações dependentes visualmente.
          let changed = true;
          while (changed) {
            changed = false;
            for (const other of choices) {
              const parent = byCode.get(other.dataset.accessParent);
              if (other.checked && !other.disabled && parent && !parent.checked) {
                other.checked = false;
                changed = true;
              }
            }
          }
          showWarning("");
        }
        updateCounts();
      });
    }

    roleField?.addEventListener("change", () => refreshRole(true));
    matrix.querySelector("[data-access-preset]")?.addEventListener("click", () => refreshRole(true));
    matrix.querySelector("[data-access-all]")?.addEventListener("click", () => {
      for (const input of choices) {
        if (!input.disabled && canCheckParent(input)) input.checked = true;
      }
      enforceDependencies();
      showWarning("");
      updateCounts();
    });
    matrix.querySelector("[data-access-none]")?.addEventListener("click", () => {
      for (const input of choices) if (!input.disabled) input.checked = false;
      showWarning("Selecione ao menos um menu antes de salvar.");
      updateCounts();
    });

    form?.addEventListener("submit", event => {
      if (locked || role() === "ADMIN") return;
      const selected = choices.filter(input => input.checked && !input.disabled);
      if (!selected.some(input => input.value.endsWith(".view"))) {
        event.preventDefault();
        showWarning("Selecione ao menos um menu principal para liberar o acesso.");
        matrix.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });

    refreshRole(false);
  }
})();