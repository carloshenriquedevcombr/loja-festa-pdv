// "Ainda estou aqui": a cada 45 s, só com a página aberta e visível. Manda só a área da tela (sem ids).
(function () {
  var url = document.body.getAttribute("data-presenca");
  var meta = document.querySelector('meta[name="csrf"]');
  var token = meta ? meta.getAttribute("content") : "";
  if (!url || !window.fetch) { return; }
  function sinal() {
    if (document.hidden) { return; }
    fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF": token },
                 body: JSON.stringify({ p: location.pathname }) }).catch(function () { /* sem internet: tenta depois */ });
  }
  sinal();
  setInterval(sinal, 45000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { sinal(); } });
})();
