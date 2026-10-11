// Tema claro/escuro do sistema: escolhe antes de desenhar a tela (sem "piscar") e guarda a escolha neste aparelho.
(function () {
  var raiz = document.documentElement, salvo = null;
  try { salvo = localStorage.getItem("tema"); } catch (e) { salvo = null; }
  if (salvo === "escuro" || salvo === "claro") { raiz.setAttribute("data-theme", salvo === "escuro" ? "dark" : "light"); }
  var menu = null;
  try { menu = localStorage.getItem("menu"); } catch (e) { menu = null; }
  if (menu === "curto") { raiz.setAttribute("data-menu", "curto"); }
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-encolher-menu]").forEach(function (b) {
      b.addEventListener("click", function () {
        var curto = raiz.getAttribute("data-menu") !== "curto";
        if (curto) { raiz.setAttribute("data-menu", "curto"); } else { raiz.removeAttribute("data-menu"); }
        try { localStorage.setItem("menu", curto ? "curto" : "normal"); } catch (e) { /* sem armazenamento */ }
      });
    });
    document.querySelectorAll("[data-alternar-tema]").forEach(function (b) {
      b.addEventListener("click", function () {
        var atual = raiz.getAttribute("data-theme") ||
          (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
        var novo = atual === "dark" ? "light" : "dark";
        raiz.setAttribute("data-theme", novo);
        try { localStorage.setItem("tema", novo === "dark" ? "escuro" : "claro"); } catch (e) { /* sem armazenamento */ }
      });
    });
  });
})();
