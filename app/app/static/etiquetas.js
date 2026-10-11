// Junta as peças marcadas num só campo "ids" (a lista vai no endereço da página de impressão).
(function () {
  var form = document.getElementById("form-etiquetas");
  if (!form) { return; }
  form.classList.add("com-js");
  form.addEventListener("submit", function (ev) {
    var ids = Array.prototype.map.call(form.querySelectorAll(".marcar-peca:checked"), function (c) { return c.value; });
    if (!ids.length) { ev.preventDefault(); alert("Marque pelo menos uma peça."); return; }
    document.getElementById("ids-escolhidas").value = ids.slice(0, 63).join(",");
  });
})();
