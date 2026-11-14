// Tema claro/escuro: aplica antes da pintura e liga o seletor do cabeçalho.
// Fica em arquivo próprio porque o CSP do site é script-src 'self' (inline é bloqueado).
(function () {
  "use strict";

  var raiz = document.documentElement;
  var tema = null;
  try {
    tema = localStorage.getItem("tema");
  } catch (e) {}
  if (tema !== "claro" && tema !== "escuro") {
    tema =
      window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "escuro"
        : "claro";
  }
  raiz.dataset.tema = tema;

  document.addEventListener("DOMContentLoaded", function () {
    var seletor = document.getElementById("tema");
    if (!seletor) return;
    seletor.value = raiz.dataset.tema || "claro";
    seletor.addEventListener("change", function () {
      raiz.dataset.tema = seletor.value;
      try {
        localStorage.setItem("tema", seletor.value);
      } catch (e) {}
      // O gráfico desenha as cores na carga; recarrega para repintar.
      if (document.getElementById("grafico-preco")) location.reload();
    });
  });
})();
