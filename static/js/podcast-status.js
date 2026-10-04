// Live sync state for podcast cards in the card list.
//
// Downloading a few episodes takes a while (podcast_sync does it in the
// background), so the freshly saved card would otherwise just sit there
// saying "waiting for download" until the admin reloads by hand. This polls
// the hub while at least one card is still working and stops on its own once
// everything is settled — no websockets, no dependencies, nothing running
// when there is nothing to watch.
(function () {
	"use strict";

	var POLL_MS = 3000;
	var BUSY_STATES = ["pending", "syncing"];

	function initPodcastStatus() {
		var configEl = document.getElementById("podcast-status-config");
		var cells = document.querySelectorAll("[data-podcast-cell]:not([data-podcast-cell=''])");
		if (!configEl || !cells.length) {
			return;
		}
		var statusUrl = JSON.parse(configEl.textContent).statusUrl;
		var timer = null;

		function isBusy() {
			return Array.prototype.some.call(cells, function (cell) {
				var badge = cell.querySelector(".podcast-sync");
				return badge && BUSY_STATES.some(function (state) {
					return badge.classList.contains("podcast-sync-" + state);
				});
			});
		}

		function apply(states) {
			Array.prototype.forEach.call(cells, function (cell) {
				var entry = states[cell.dataset.podcastCell];
				if (!entry) {
					return;
				}
				var badge = cell.querySelector(".podcast-sync");
				if (badge) {
					badge.textContent = entry.text;
					badge.className = "podcast-sync podcast-sync-" + entry.state;
				}
				var label = cell.querySelector(".mh-content-label");
				if (label) {
					label.textContent = entry.label;
				}
			});
		}

		function poll() {
			fetch(statusUrl)
				.then(function (response) {
					return response.ok ? response.json() : null;
				})
				.then(function (data) {
					if (data) {
						apply(data.cards || {});
					}
				})
				.catch(function () {
					// A failed poll is not worth reporting — the next page load
					// shows the real state anyway.
				})
				.finally(function () {
					if (isBusy()) {
						timer = window.setTimeout(poll, POLL_MS);
					} else {
						timer = null;
					}
				});
		}

		if (isBusy()) {
			timer = window.setTimeout(poll, POLL_MS);
		}

		// Don't keep polling a tab nobody is looking at.
		document.addEventListener("visibilitychange", function () {
			if (document.hidden && timer) {
				window.clearTimeout(timer);
				timer = null;
			} else if (!document.hidden && !timer && isBusy()) {
				timer = window.setTimeout(poll, POLL_MS);
			}
		});
	}

	document.addEventListener("DOMContentLoaded", initPodcastStatus);
})();
