/* sideinstaller.net: one script for every page. Each part looks for the
   elements it drives and does nothing on a page without them. Every page
   reads and works with this file missing; it adds the motion, the phone's
   run, the certificate list's sorting and the terms gate. */
(function () {
  "use strict";

  var doc = document;
  function $(sel, root) { return (root || doc).querySelector(sel); }
  function $$(sel, root) { return [].slice.call((root || doc).querySelectorAll(sel)); }
  // The icon sprite at the address this page links it at, version and all.
  var firstIcon = doc.querySelector("use[href*='icons.svg']");
  var SPRITE = firstIcon ? firstIcon.getAttribute("href").split("#")[0] : "assets/icons.svg";
  var reduce = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false };

  /* ---------- navigation ---------- */

  var nav = $(".nav");
  if (nav) {
    var onScroll = function () { nav.classList.toggle("is-scrolled", window.scrollY > 8); };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();

    var menu = $(".nav-menu", nav);
    var setMenu = function (open) {
      nav.classList.toggle("is-open", open);
      menu.setAttribute("aria-expanded", open ? "true" : "false");
    };
    if (menu) {
      menu.addEventListener("click", function () { setMenu(!nav.classList.contains("is-open")); });
      doc.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && nav.classList.contains("is-open")) { setMenu(false); menu.focus(); }
      });
      doc.addEventListener("click", function (e) {
        if (!nav.classList.contains("is-open")) return;
        // A link in the menu, or a tap anywhere outside it.
        if (!nav.contains(e.target) || (e.target.closest && e.target.closest(".nav-links a"))) setMenu(false);
      });
    }
  }

  /* ---------- blocks that fade up as they scroll into view ---------- */

  var hiddenBlocks = $$("[data-reveal]");
  if ("IntersectionObserver" in window && !reduce.matches) {
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("in");
        seen.unobserve(entry.target);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    hiddenBlocks.forEach(function (el) { seen.observe(el); });
  } else {
    hiddenBlocks.forEach(function (el) { el.classList.add("in"); });
  }

  /* ---------- videos ----------
   A [data-film] link plays its video over the page. It is a plain link to
   YouTube, so it still goes somewhere without this. The player is only
   loaded once it is asked for. */

  function player(id, title, autoplay) {
    var frame = doc.createElement("iframe");
    frame.src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(id) + "?rel=0" + (autoplay ? "&autoplay=1" : "");
    frame.title = title;
    frame.loading = "lazy";
    frame.allow = "accelerometer; autoplay; encrypted-media; gyroscope; picture-in-picture; web-share; fullscreen";
    frame.setAttribute("allowfullscreen", "");
    frame.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
    return frame;
  }

  var film = null;
  function openFilm(id, title) {
    if (!film) {
      film = doc.createElement("dialog");
      film.className = "film";
      film.innerHTML =
        '<button type="button" class="film-close" aria-label="Close">' +
        '<svg class="icon" aria-hidden="true"><use href="' + SPRITE + '#x"/></svg></button>' +
        '<div class="film-frame"></div>';
      doc.body.appendChild(film);
      film.addEventListener("click", function (e) {
        if (e.target === film || (e.target.closest && e.target.closest(".film-close"))) film.close();
      });
      // Dropping the player is what stops the sound.
      film.addEventListener("close", function () { $(".film-frame", film).textContent = ""; });
    }
    film.setAttribute("aria-label", title);
    $(".film-frame", film).appendChild(player(id, title, true));
    film.showModal();
  }
  doc.addEventListener("click", function (e) {
    var link = e.target.closest && e.target.closest("[data-film]");
    if (!link || !window.HTMLDialogElement) return;
    e.preventDefault();
    openFilm(link.getAttribute("data-film"), link.getAttribute("data-film-title") || "Video");
  });

  /* ---------- the phone's run (Overview) ----------
   "How it works" holds the four steps and a copy of the hero's phone on one
   stage. The stage stays pinned while the page scrolls through the track
   under it, and the scroll plays the run: each step takes a quarter of the
   track, and everything on the phone is set from how far into its step the
   page is, so scrolling back plays it backwards. Picking a step scrolls to
   it. With reduced motion nothing is pinned, and picking a step shows it. */

  var heroPhone = $("#phone"), stage = $("#demo"), stepList = $("#steps"), track = $("#howTrack");
  if (heroPhone && stage && stepList && track) (function () {
    var phone = heroPhone.cloneNode(true);
    phone.removeAttribute("id");
    phone.classList.add("is-scrub");
    phone.setAttribute("aria-label", "SideInstaller on an iPhone, going through an install");
    stage.appendChild(phone);

    var pin = $(".how-stage", track);
    var steps = $$(".step", stepList);
    var bar = $("#stepBar");
    var segments = [];
    var ui = {
      state: $(".p-state", phone), stateText: $(".p-state span", phone),
      runTitle: $(".p-run-head b", phone), pct: $(".p-pct", phone), fill: $(".p-bar i", phone),
      stepTitle: $(".p-step-text b", phone), stepCount: $(".p-step-text small", phone),
      sheet: $(".p-sheet", phone), scrim: $(".p-scrim", phone), popup: $(".p-popup", phone),
      email: $(".p-input", phone), password: $(".p-input.is-secret", phone)
    };
    var EMAIL = ui.email.textContent, DOTS = ui.password.textContent;

    // The app's own checklist for a SideStore install.
    var RUN = ["Connect the VPN", "Get pairing file", "Open the device link", "Sign in to Apple ID",
               "Download SideStore", "Sign the app", "Install SideStore", "Finish setup"];
    // Where a picked step lands, as a share of it: the moment that shows it.
    var LAND = [1, 0.62, 0.62, 1];
    // The share of the track the run takes; the rest holds the last frame.
    var RUN_SHARE = 0.92;

    function clamp(x) { return x < 0 ? 0 : x > 1 ? 1 : x; }
    function span(t, from, to) { return clamp((t - from) / (to - from)); }
    function ease(x) { return 1 - Math.pow(1 - x, 3); }
    function text(node, value) { if (node.textContent !== value) node.textContent = value; }
    function flag(name, on) { phone.classList.toggle(name, on); }

    var drawn = "";
    function render(n, t) {
      var key = n + ":" + t.toFixed(4);
      if (key === drawn) return;
      drawn = key;

      steps.forEach(function (step, i) {
        step.classList.toggle("is-active", i === n);
        step.classList.toggle("is-past", i < n);
        step.style.setProperty("--fill", i < n ? 1 : i === n ? t : 0);
        $(".step-btn", step).setAttribute("aria-current", i === n ? "step" : "false");
      });
      segments.forEach(function (seg, i) {
        seg.style.setProperty("--fill", i < n ? 1 : i === n ? 0.08 + 0.92 * t : 0);
      });

      // 1. LocalDevVPN: the tunnel comes up.
      var up = n > 0 || t > 0.4;
      ui.state.classList.toggle("is-off", !up);
      text(ui.stateText, up ? "Tunnel connected" : "Tunnel off");

      // 2. The Apple ID sheet rises, is filled in, Continue, and it goes.
      var sheet = n !== 1 ? 0 : t < 0.5 ? ease(span(t, 0.04, 0.3)) : 1 - ease(span(t, 0.84, 1));
      ui.sheet.style.transform = "translateY(" + ((1 - sheet) * 102).toFixed(2) + "%)";
      var typed = n > 1 ? 1 : n === 1 ? span(t, 0.3, 0.56) : 0;
      var dots = n > 1 ? 1 : n === 1 ? span(t, 0.58, 0.7) : 0;
      text(ui.email, EMAIL.slice(0, Math.round(EMAIL.length * typed)));
      text(ui.password, DOTS.slice(0, Math.round(DOTS.length * dots)));
      flag("is-continue", n === 1 && t > 0.74 && t < 0.84);

      // 3. Install is tapped, the run starts, and stops for the pairing code.
      flag("is-pressed", n === 2 && t > 0.03 && t < 0.12);
      flag("is-running", n === 3 || (n === 2 && t >= 0.12));
      var pop = n !== 2 ? 0 : Math.min(span(t, 0.4, 0.5), 1 - span(t, 0.82, 0.9));
      ui.popup.style.opacity = pop.toFixed(3);
      ui.popup.style.transform = "translateY(calc(" + ((1 - pop) * 12).toFixed(2) + " * var(--p))) scale(" +
        (0.97 + 0.03 * pop).toFixed(4) + ")";
      flag("is-waiting", n === 2 && t >= 0.42 && t < 0.86);
      ui.scrim.style.opacity = Math.max(sheet, pop).toFixed(3);

      // 4. The rest of the run, through to Installed.
      var done = n === 3 && t >= 0.86;
      var p = n < 2 ? 0 : n === 2 ? 0.12 * span(t, 0.12, 0.38) : 0.12 + 0.88 * span(t, 0, 0.86);
      var at = n < 2 ? 0 : n === 2 ? (t < 0.26 ? 0 : 1) : done ? 7 : Math.min(2 + Math.floor(span(t, 0, 0.86) * 6), 7);
      ui.fill.style.transform = "scaleX(" + Math.max(p, 0.02).toFixed(4) + ")";
      text(ui.pct, Math.round(p * 100) + "%");
      text(ui.stepTitle, RUN[at]);
      text(ui.stepCount, "Step " + (at + 1) + " of " + RUN.length);
      text(ui.runTitle, done ? "Installed" : "Installing");
      flag("is-done", done);
    }

    var scrolling = !reduce.matches;

    // How far the stage has travelled while pinned, from 0 to 1.
    function pinTop() { return parseFloat(getComputedStyle(pin).top) || 0; }
    function travel() { return Math.max(1, track.offsetHeight - pin.offsetHeight); }
    function fromScroll() {
      var g = clamp(clamp((pinTop() - track.getBoundingClientRect().top) / travel()) / RUN_SHARE) * 4;
      var n = Math.min(3, Math.floor(g));
      render(n, g - n);
    }
    var queued = false;
    function onScroll() {
      if (queued) return;
      queued = true;
      requestAnimationFrame(function () { queued = false; fromScroll(); });
    }

    function pick(n) {
      if (!scrolling) { render(n, LAND[n]); return; }
      var into = (n + (n === 3 ? 0.95 : 0.55)) / 4 * RUN_SHARE;
      window.scrollTo({
        top: track.getBoundingClientRect().top + window.scrollY - pinTop() + into * travel(),
        behavior: "smooth"
      });
    }

    steps.forEach(function (step, i) {
      $(".step-btn", step).addEventListener("click", function () { pick(i); });
    });
    if (bar) {
      steps.forEach(function (step, i) {
        var seg = doc.createElement("button");
        seg.type = "button";
        seg.setAttribute("aria-label", "Step " + (i + 1) + ": " + $(".step-text b", step).textContent);
        seg.addEventListener("click", function () { pick(i); });
        bar.appendChild(seg);
        segments.push(seg);
      });
    }

    if (scrolling) {
      track.classList.add("is-scroll");
      window.addEventListener("scroll", onScroll, { passive: true });
      window.addEventListener("resize", onScroll);
      fromScroll();
    } else {
      render(0, LAND[0]);
    }
  })();

  /* ---------- the certificate list and the terms gate (Install) ---------- */

  var certs = $("#certs");
  if (certs) (function () {
    var q = $("#q"), sortSel = $("#sort"), empty = $("#empty");
    var rows = $$(".cert", certs);
    function days(c) { var d = parseInt(c.getAttribute("data-days"), 10); return isNaN(d) ? -1e9 : d; }
    function name(c) { return (c.getAttribute("data-name") || "").toLowerCase(); }
    /* 0 = Apple still honours it, 1 = never checked, 2 = revoked or expired. */
    function rank(c) { var r = parseInt(c.getAttribute("data-rank"), 10); return isNaN(r) ? 1 : r; }

    function apply() {
      var term = q ? (q.value || "").trim().toLowerCase() : "";
      var mode = sortSel ? sortSel.value : "days";
      var sorted = rows.slice().sort(function (a, b) {
        /* Rank leads in every mode. Sorting by name or by expiry is a way to
           find one certificate among many, never a reason to be handed a dead
           one first. */
        if (rank(a) !== rank(b)) return rank(a) - rank(b);
        if (mode === "name") return name(a).localeCompare(name(b));
        if (mode === "days-asc") return days(a) - days(b);
        return days(b) - days(a);
      });
      var visible = 0, lead = null;
      sorted.forEach(function (c) {
        var show = !term || name(c).indexOf(term) > -1;
        c.hidden = !show;
        c.classList.remove("is-lead");
        if (show) {
          visible++;
          // The first certificate Apple still honours is the one to tap.
          if (!lead && rank(c) === 0) lead = c;
        }
        certs.appendChild(c);
      });
      if (lead) lead.classList.add("is-lead");
      certs.hidden = rows.length > 0 && visible === 0;
      if (empty) empty.hidden = !(rows.length > 0 && visible === 0);
    }

    /* The page is only rebuilt when a certificate changes, so the days left
       baked into it go stale. Recount them from each row's expiry on every
       visit; a certificate that has run out since loses its Valid mark and
       sinks with the dead ones. */
    rows.forEach(function (c) {
      var t = Date.parse(c.getAttribute("data-expires") || "");
      if (isNaN(t)) return;
      var s = (t - Date.now()) / 864e5, d = s >= 0 ? Math.ceil(s) : Math.floor(s);
      c.setAttribute("data-days", d);
      var el = $(".days", c);
      if (el) {
        el.className = "days " + (d < 0 ? "bad" : d <= 7 ? "crit" : d <= 30 ? "warn" : "good");
        el.textContent = d < 0 ? "Expired" : d === 0 ? "Expires today" : d === 1 ? "1 day left" : d + " days left";
      }
      if (d < 0) {
        c.setAttribute("data-rank", "2");
        var status = $(".status", c);
        if (status) {
          var sep = status.nextElementSibling;
          if (sep && sep.classList.contains("sep")) sep.parentNode.removeChild(sep);
          status.parentNode.removeChild(status);
        }
      }
    });
    if (q) q.addEventListener("input", apply);
    if (sortSel) sortSel.addEventListener("change", apply);
    apply();

    /* Terms gate: the steps stay dimmed, and the certificate installs inert,
       until the box is ticked. Acceptance is remembered per device. */
    var tos = $("#tosCheck"), flow = $("#flow");
    var TOS_KEY = "si-tos-accepted";
    if (tos && flow) {
      var locked = function () { return !tos.checked; };
      var applyTos = function () {
        flow.classList.toggle("is-locked", locked());
        certs.classList.toggle("is-locked", locked());
        certs.setAttribute("aria-disabled", locked() ? "true" : "false");
        if ("inert" in certs) certs.inert = locked();
        try {
          if (tos.checked) localStorage.setItem(TOS_KEY, "1");
          else localStorage.removeItem(TOS_KEY);
        } catch (e) {}
      };
      try { if (localStorage.getItem(TOS_KEY) === "1") tos.checked = true; } catch (e) {}
      tos.addEventListener("change", applyTos);
      /* Belt and braces for browsers without inert: swallow activations
         (including keyboard Enter) while locked. */
      certs.addEventListener("click", function (e) {
        if (locked()) { e.preventDefault(); e.stopPropagation(); }
      }, true);
      applyTos();
    }
  })();

  /* SideInstaller only installs onto the device this page is open on, so
     anything that isn't an iPhone or iPad is pointed to one. iPadOS reports
     itself as a Mac, so count touch points. */
  var handoff = $("#handoff");
  if (handoff) {
    var ua = navigator.userAgent;
    var appleTouch = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
    handoff.hidden = appleTouch;
  }

  /* ---------- tutorials (Help) ---------- */

  var tutorials = $("#tutorialVideos");
  if (tutorials) (function () {
    var VIDEOS = {
      en: { sidestore: ["4J4muoczR54", "OyeYTSEheQw"], jit: ["U90-4jA8Kj8", "WCYkxfRHapg"] },
      es: { sidestore: ["tXGihEkcg-4"], jit: ["hEhkDEALNWo"] }
    };
    var STRINGS = {
      en: { sidestore: "Installing SideStore", jit: "Enabling JIT", part: "Part", watch: "Watch on YouTube" },
      es: { sidestore: "Instalar SideStore", jit: "Activar JIT", part: "Parte", watch: "Ver en YouTube" }
    };
    var lang = "en", topic = "sidestore";

    function build() {
      var ids = VIDEOS[lang][topic] || [], s = STRINGS[lang];
      tutorials.textContent = "";
      ids.forEach(function (id, i) {
        var title = ids.length > 1 ? s[topic] + ", " + s.part.toLowerCase() + " " + (i + 1) : s[topic];
        var figure = doc.createElement("figure");
        var frame = doc.createElement("div");
        frame.className = "film-frame";
        frame.appendChild(player(id, title, false));
        var caption = doc.createElement("figcaption");
        var label = doc.createElement("span");
        label.textContent = title;
        var link = doc.createElement("a");
        link.className = "link";
        link.href = "https://youtu.be/" + id;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = s.watch;
        caption.appendChild(label);
        caption.appendChild(link);
        figure.appendChild(frame);
        figure.appendChild(caption);
        tutorials.appendChild(figure);
      });
    }

    $$("[data-pick]").forEach(function (group) {
      var kind = group.getAttribute("data-pick");
      group.addEventListener("click", function (e) {
        var btn = e.target.closest && e.target.closest("button[data-value]");
        if (!btn) return;
        if (kind === "lang") lang = btn.getAttribute("data-value");
        else topic = btn.getAttribute("data-value");
        $$("button", group).forEach(function (b) { b.setAttribute("aria-pressed", b === btn ? "true" : "false"); });
        build();
      });
    });
    build();
  })();

  /* ---------- crypto donations (Help): one wallet at a time, by tab ---------- */

  var coins = $("#coins");
  if (coins) (function () {
    var tabs = $$("[role=tab]", coins);
    function showCoin(tab) {
      tabs.forEach(function (b) {
        var on = b === tab;
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
        doc.getElementById(b.getAttribute("aria-controls")).hidden = !on;
      });
    }
    /* Left and right arrows move along the tabs, as in any tablist. */
    $("[role=tablist]", coins).addEventListener("keydown", function (e) {
      var i = tabs.indexOf(doc.activeElement);
      var dir = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (i < 0 || !dir) return;
      e.preventDefault();
      var next = tabs[(i + dir + tabs.length) % tabs.length];
      showCoin(next);
      next.focus();
    });

    function copied(btn) {
      var label = $("span", btn);
      label.textContent = "Copied";
      clearTimeout(btn.copyTimer);
      btn.copyTimer = setTimeout(function () { label.textContent = "Copy"; }, 1600);
    }
    /* The async clipboard needs a secure context; elsewhere fall back to
       execCommand on a selection, which also leaves the address selected for
       copying by hand if even that is refused. */
    function copyAddress(btn) {
      var code = $("code", btn.parentNode);
      function fallback() {
        var range = doc.createRange();
        range.selectNodeContents(code);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        var ok = false;
        try { ok = doc.execCommand("copy"); } catch (e) {}
        if (ok) copied(btn);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(code.textContent).then(function () { copied(btn); }, fallback);
      } else {
        fallback();
      }
    }
    coins.addEventListener("click", function (e) {
      if (!e.target.closest) return;
      var tab = e.target.closest("[role=tab]"), copy = e.target.closest(".copy-btn");
      if (tab) showCoin(tab);
      else if (copy) copyAddress(copy);
    });
  })();
})();
