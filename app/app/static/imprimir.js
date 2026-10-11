// Botão "Imprimir" da etiqueta (arquivo separado: a política de segurança não permite script na página).
document.querySelectorAll("[data-imprimir]").forEach(function (b) {
  b.addEventListener("click", function () { window.print(); });
});
