// Painel: mostra o valor do dia ao passar o dedo/mouse ou focar uma coluna do gráfico.
(function () {
  document.querySelectorAll(".bloco-painel").forEach(function (bloco) {
    var dica = bloco.querySelector(".grafico-dica");
    if (!dica) { return; }
    bloco.querySelectorAll("g.barra-dia").forEach(function (g) {
      function mostrar() { dica.textContent = g.getAttribute("data-dica"); }
      g.addEventListener("mouseenter", mostrar);
      g.addEventListener("focus", mostrar);
      g.addEventListener("click", mostrar);
    });
  });
})();
