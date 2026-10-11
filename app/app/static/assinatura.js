// Quadro de assinatura (dedo, caneta ou mouse). Ao confirmar, a imagem vai num campo escondido.
(function () {
  var quadro = document.getElementById("quadro-assinatura");
  if (!quadro || !quadro.getContext) return;
  var campo = document.getElementById("assinatura");
  var form = quadro.closest("form");
  var ctx = quadro.getContext("2d");
  var desenhando = false, tracos = 0, ultimo = null;

  function ajustar() {
    var r = quadro.getBoundingClientRect();
    var escala = Math.min(window.devicePixelRatio || 1, 2);
    quadro.width = Math.max(300, Math.min(1600, Math.round(r.width * escala)));
    quadro.height = Math.max(120, Math.min(700, Math.round(r.height * escala)));
    ctx.lineWidth = 2.6 * escala; ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.strokeStyle = "#111";
    tracos = 0;
  }
  function ponto(ev) {
    var r = quadro.getBoundingClientRect();
    return {x: (ev.clientX - r.left) * quadro.width / r.width, y: (ev.clientY - r.top) * quadro.height / r.height};
  }
  quadro.addEventListener("pointerdown", function (ev) {
    ev.preventDefault(); desenhando = true; ultimo = ponto(ev); quadro.setPointerCapture(ev.pointerId);
  });
  quadro.addEventListener("pointermove", function (ev) {
    if (!desenhando) return;
    var p = ponto(ev);
    ctx.beginPath(); ctx.moveTo(ultimo.x, ultimo.y); ctx.lineTo(p.x, p.y); ctx.stroke();
    ultimo = p; tracos++;
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach(function (n) {
    quadro.addEventListener(n, function () { desenhando = false; });
  });
  document.getElementById("apagar-assinatura").addEventListener("click", function () {
    ctx.clearRect(0, 0, quadro.width, quadro.height); tracos = 0;
  });
  form.addEventListener("submit", function (ev) {
    if (tracos < 5) { ev.preventDefault(); alert("Faça a assinatura no quadro antes de confirmar."); return; }
    campo.value = quadro.toDataURL("image/png");
  });
  ajustar();
  window.addEventListener("resize", function () { if (!tracos) ajustar(); });
  form.classList.add("com-js");
})();
