// Confirmação de exclusão: inline handler é bloqueado pelo CSP (script-src 'self').
document.addEventListener(
  "submit",
  function (evento) {
    var formulario = evento.target;
    var pergunta = formulario && formulario.getAttribute("data-confirmar");
    if (pergunta && !window.confirm(pergunta)) evento.preventDefault();
  },
  true
);
