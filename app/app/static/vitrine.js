// Banners de promoção: passam sozinhos a cada N segundos (param quando a pessoa mexe ou pede menos movimento).
(function () {
  var caixa = document.querySelector(".banners[data-girar]");
  if (!caixa) { return; }
  var trilho = caixa.querySelector(".banners-trilho");
  var pontos = caixa.querySelectorAll(".banners-pontos a");
  var total = trilho.children.length;
  var segundos = parseInt(caixa.getAttribute("data-girar"), 10) || 7;
  var parado = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function atual() { return Math.round(trilho.scrollLeft / Math.max(trilho.clientWidth, 1)); }
  function marcar() {
    var i = atual();
    pontos.forEach(function (p, n) { p.classList.toggle("atual", n === i); });
  }
  function ir(i) { trilho.scrollTo({ left: i * trilho.clientWidth }); }
  pontos.forEach(function (p, n) {
    p.addEventListener("click", function (ev) { ev.preventDefault(); parado = true; ir(n); });
  });
  trilho.addEventListener("scroll", marcar, { passive: true });
  ["pointerdown", "touchstart", "focusin"].forEach(function (e) {
    caixa.addEventListener(e, function () { parado = true; }, { passive: true });
  });
  marcar();
  if (total < 2) { return; }
  setInterval(function () { if (!parado && !document.hidden) { ir((atual() + 1) % total); } }, segundos * 1000);
})();
