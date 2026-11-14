// Menu hambúrguer em telas pequenas. Fica em arquivo próprio porque o CSP do
// site é script-src 'self' (inline é bloqueado em silêncio).
(function () {
  "use strict";

  function iniciar() {
    var botao = document.querySelector(".botao-menu");
    var cabecalho = document.querySelector(".cabecalho");
    if (!botao || !cabecalho) return;

    function fechar() {
      cabecalho.classList.remove("menu-aberto");
      botao.setAttribute("aria-expanded", "false");
    }

    botao.addEventListener("click", function () {
      var aberto = cabecalho.classList.toggle("menu-aberto");
      botao.setAttribute("aria-expanded", aberto ? "true" : "false");
    });

    document.addEventListener("keydown", function (evento) {
      if (evento.key !== "Escape" || !cabecalho.classList.contains("menu-aberto")) {
        return;
      }
      fechar();
      botao.focus();
    });

    var nav = cabecalho.querySelector("nav");
    if (nav) {
      nav.addEventListener("click", function (evento) {
        if (evento.target && evento.target.closest("a")) fechar();
      });
    }

    // O bfcache restaura o DOM com o menu possivelmente aberto.
    window.addEventListener("pageshow", fechar);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", iniciar);
  } else {
    iniciar();
  }
})();
