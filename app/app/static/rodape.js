// Rodapé: mapa do Google só carrega quando a pessoa pede (mais rápido e sem rastreio antes do toque)
// e o cadastro de novidades envia sem sair da página.
(function () {
  function abrirMapa(caixa) {
    var botao = caixa.querySelector(".ver-mapa");
    var url = caixa.getAttribute("data-embed") || "";
    if (!botao || url.indexOf("https://www.google.com/maps") !== 0) { return; }
    var quadro = document.createElement("iframe");
    quadro.src = url;
    quadro.title = "Mapa da loja";
    quadro.loading = "lazy";
    quadro.referrerPolicy = "no-referrer";
    quadro.className = "mapa-quadro";
    caixa.insertBefore(quadro, botao);
    botao.remove();
  }
  document.querySelectorAll(".mapa").forEach(function (caixa) {
    var botao = caixa.querySelector(".ver-mapa");
    if (botao) { botao.addEventListener("click", function () { abrirMapa(caixa); }); }
  });
  // O mapa da loja principal aparece sozinho quando a pessoa chega no rodapé (sem pesar o começo da página)
  var auto = document.querySelectorAll(".mapa-auto");
  if (auto.length && "IntersectionObserver" in window) {
    var olho = new IntersectionObserver(function (vistos) {
      vistos.forEach(function (v) { if (v.isIntersecting) { abrirMapa(v.target); olho.unobserve(v.target); } });
    }, { rootMargin: "300px" });
    auto.forEach(function (c) { olho.observe(c); });
  } else { auto.forEach(abrirMapa); }
  var form = document.getElementById("form-novidades");
  if (!form) { return; }
  var resposta = form.querySelector(".novidades-resposta");
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    resposta.textContent = "Enviando…";
    fetch(form.action, { method: "POST", body: new FormData(form), credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        resposta.textContent = d.ok ? d.texto : (d.erro || "Não foi possível agora.");
        if (d.ok) { form.reset(); }
      })
      .catch(function () { resposta.textContent = "Sem conexão. Tente de novo."; });
  });
})();

// Botão "Copiar" (Pix copia e cola e afins): data-copiar="#id-do-campo"
document.querySelectorAll("[data-copiar]").forEach(function (b) {
  b.addEventListener("click", function () {
    var campo = document.querySelector(b.getAttribute("data-copiar"));
    if (!campo) { return; }
    var texto = campo.value || campo.textContent || "";
    var feito = function () { var antes = b.textContent; b.textContent = "Copiado ✓"; setTimeout(function () { b.textContent = antes; }, 2500); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(texto).then(feito, function () { campo.select(); document.execCommand("copy"); feito(); });
    } else { campo.select(); document.execCommand("copy"); feito(); }
  });
});
