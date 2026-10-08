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
