// Pequenos ajustes do sistema: fechar o menu lateral com Esc (celular).
(function () {
  document.addEventListener("keydown", function (ev) {
    var c = document.getElementById("abrir-lateral");
    if (ev.key === "Escape" && c && c.checked) { c.checked = false; }
  });
})();
