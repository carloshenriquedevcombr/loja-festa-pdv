// Chat do site: a conversa vai para o WhatsApp da loja e as respostas voltam aqui.
(function () {
  var caixa = document.getElementById("chat-site");
  if (!caixa || !window.fetch) return;
  var lista = caixa.querySelector(".chat-mensagens");
  var form = caixa.querySelector("form");
  var inicio = caixa.querySelector(".chat-inicio");
  var erro = caixa.querySelector(".chat-erro");
  var abrir = document.getElementById("abrir-chat");
  var ultimo = 0, temConversa = false, timer = null;

  function mostrar(msgs) {
    msgs.forEach(function (m) {
      if (m.id <= ultimo) return;
      ultimo = m.id;
      var p = document.createElement("div");
      p.className = "chat-msg " + (m.de === "CLIENTE" ? "minha" : "loja");
      var t = document.createElement("span");
      t.textContent = m.texto;                       // textContent: nada de HTML vindo de fora
      var h = document.createElement("small");
      h.textContent = (m.de === "LOJA" && m.autor ? m.autor + " · " : "") + m.hora;
      p.appendChild(t); p.appendChild(h);
      lista.appendChild(p);
    });
    lista.scrollTop = lista.scrollHeight;
  }
  function conversando(sim) {
    temConversa = sim;
    if (inicio) inicio.hidden = sim;
  }
  function buscar() {
    fetch("/site/chat/mensagens?depois=" + ultimo, {credentials: "same-origin"})
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d.conversa) { conversando(true); mostrar(d.mensagens); } })
      .catch(function () {});
  }
  function ritmo() {
    clearInterval(timer);
    timer = setInterval(function () { if (abrir.checked && temConversa) buscar(); }, 4000);
  }
  abrir.addEventListener("change", function () { if (abrir.checked) buscar(); });
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    erro.textContent = "";
    var dados = new URLSearchParams(new FormData(form));
    dados.set("depois", ultimo);
    var botao = form.querySelector("button"); botao.disabled = true;
    fetch("/site/chat/enviar", {method: "POST", body: dados, credentials: "same-origin",
                                headers: {"Content-Type": "application/x-www-form-urlencoded"}})
      .then(function (r) { return r.json().then(function (d) { return [r.ok, d]; }); })
      .then(function (res) {
        botao.disabled = false;
        if (!res[0]) { erro.textContent = res[1].erro || "Não foi possível enviar."; return; }
        form.querySelector("[name=texto]").value = "";
        conversando(true); mostrar(res[1].mensagens); ritmo();
      })
      .catch(function () { botao.disabled = false; erro.textContent = "Sem conexão. Tente de novo."; });
  });
  caixa.classList.add("com-js");
  buscar(); ritmo();
})();
