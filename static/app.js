// The dashboard's only script. Loaded from a file, not inline, so the
// Content-Security-Policy (see app.py) can forbid inline script outright.
(function () {
  "use strict";

  // Delete asks first. The restaurant's name travels as a data attribute, so
  // it reaches the dialog as plain text -- no string-escaping to get wrong.
  document.addEventListener("submit", function (e) {
    var name = e.target.getAttribute("data-confirm-delete");
    if (name === null) { return; }
    if (!confirm("Stop watching " + name + "? Its check history will be " +
                 "deleted too, and this cannot be undone.")) {
      e.preventDefault();
    }
  });

  // One press per POST. Once a form is on its way its buttons go dead and a
  // second submit (double-click, Enter held down) is dropped, so a slow Places
  // call can't be paid for twice. Registered after the confirm handlers above,
  // so a cancelled dialog (defaultPrevented) leaves the form usable. Only a
  // convenience: the server-side limits are the real guard.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || form.method.toLowerCase() !== "post") { return; }
    if (form.hasAttribute("data-submitting")) {
      e.preventDefault();
      return;
    }
    form.setAttribute("data-submitting", "");
    // After the browser has read the form, and via form.elements so buttons
    // attached with form="..." (the bulk verify) are included.
    setTimeout(function () {
      Array.prototype.forEach.call(form.elements, function (el) {
        if (el.type === "submit" && !el.disabled) {
          el.disabled = true;
          el.setAttribute("data-was-enabled", "");
        }
      });
    }, 0);
  });

  // Back/forward can restore this page from memory with the buttons still
  // dead; undo only the ones this script disabled (not the cooldown's).
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) { return; }
    document.querySelectorAll("[data-submitting]").forEach(function (f) {
      f.removeAttribute("data-submitting");
    });
    document.querySelectorAll("[data-was-enabled]").forEach(function (b) {
      b.disabled = false;
      b.removeAttribute("data-was-enabled");
    });
  });

  // "Select all" on the needs-verifying card, and a count to confirm before
  // the bulk verify goes through.
  var all = document.getElementById("select-all");
  var bulk = document.getElementById("bulk-verify");
  if (all && bulk) {
    var picks = document.querySelectorAll("input.pick");
    all.addEventListener("change", function () {
      picks.forEach(function (p) { p.checked = all.checked; });
    });
    bulk.addEventListener("submit", function (e) {
      var n = document.querySelectorAll("input.pick:checked").length;
      if (n && !confirm("Confirm that " + n + " restaurant" +
                        (n === 1 ? " is" : "s are") + " the right place?")) {
        e.preventDefault();
      }
    });
  }
})();
