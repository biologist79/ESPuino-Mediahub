// Podcast picker for the card assignment form: load a feed, then pick either
// "always the newest" or a fixed set of episodes.
//
// Same house rules as media-browser.js — vanilla JS, no framework, no CDN,
// the feed is fetched through the hub's own /podcast/feed endpoint (so the
// browser never talks to the feed host directly). Everything the form
// submits ends up in one hidden JSON field; the server re-validates it
// (_parse_podcast_form).
(function () {
	"use strict";

	function initPodcastPicker(section) {
		var config = JSON.parse(document.getElementById("podcast-config").textContent);
		var labels = config.labels;
		var singleFileModes = (config.singleFileModes || []).map(String);

		var feedInput = document.getElementById("podcast-feed-url");
		var feedBtn = document.getElementById("podcast-feed-btn");
		var feedStatus = document.getElementById("podcast-feed-status");
		var showRow = document.getElementById("podcast-show-row");
		var showEl = document.getElementById("podcast-show");
		var selectionRow = document.getElementById("podcast-selection-row");
		var latestRadio = document.getElementById("podcast-selection-latest");
		var episodesRadio = document.getElementById("podcast-selection-episodes");
		var countRow = document.getElementById("podcast-count-row");
		var countInput = document.getElementById("podcast-count");
		var episodesEl = document.getElementById("podcast-episodes");
		var episodesNote = document.getElementById("podcast-episodes-note");
		var playModeRow = document.getElementById("podcast-play-mode-row");
		var playModeSelect = document.getElementById("podcast_play_mode");
		var hiddenInput = document.getElementById("podcast-json");

		// The whole picker state, mirrored into the hidden field on every change.
		var state = {
			feed_url: null,
			show_title: "",
			show_image: null,
			selection: "latest",
			episode_count: 1,
			episodes: []
		};
		// The feed's episodes, newest first. A feed arrives whole, so there is
		// nothing to page through.
		var loadedEpisodes = [];

		function isSingleFileMode() {
			return singleFileModes.indexOf(playModeSelect.value) !== -1;
		}

		function save() {
			hiddenInput.value = JSON.stringify(state);
		}

		function text(tag, className, content) {
			var el = document.createElement(tag);
			if (className) {
				el.className = className;
			}
			if (content !== undefined && content !== null) {
				el.textContent = content;
			}
			return el;
		}

		function formatDate(iso) {
			if (!iso) {
				return "";
			}
			var date = new Date(iso);
			return isNaN(date.getTime()) ? String(iso).slice(0, 10) : date.toLocaleDateString();
		}

		function formatDuration(seconds) {
			if (!seconds) {
				return "";
			}
			var minutes = Math.round(seconds / 60);
			return minutes + " min";
		}

		// Feeds state the enclosure size, and a 90 MB episode is a very
		// different proposition for an ESPuino than a 5 MB one — worth seeing
		// before a card is committed to it. Sources that don't report a size
		// simply leave the field out.
		function formatSize(bytes) {
			if (!bytes) {
				return "";
			}
			var mb = bytes / 1048576;
			return (mb >= 10 ? Math.round(mb) : Math.round(mb * 10) / 10) + " MB";
		}

		// "25 episodes · latest episode: 21/09/2026" — the date is what tells
		// you at a glance whether a podcast is still running or was last
		// touched years ago, which the episode count alone does not.
		function showMeta(show) {
			var parts = [];
			if (show.episode_count) {
				parts.push(labels.episodeCount.replace("{num}", show.episode_count));
			}
			if (show.last_item_added) {
				parts.push(labels.latestEpisode.replace("{date}", formatDate(show.last_item_added)));
			}
			return parts.join(" · ");
		}

		function setMessage(container, message, isError) {
			container.hidden = false;
			container.innerHTML = "";
			container.appendChild(text("p", isError ? "podcast-msg podcast-error" : "podcast-msg muted", message));
		}

		// -- podcast feed --------------------------------------------------
		// `keepSelection` is set when re-opening a saved card: the feed is
		// fetched only to fill in title, cover and episode list, and must not
		// discard the episodes the admin picked earlier.
		function loadFeed(keepSelection) {
			var url = feedInput.value.trim();
			if (!url) {
				return;
			}
			setMessage(feedStatus, labels.loadingFeed, false);
			fetch(config.feedUrl + "?url=" + encodeURIComponent(url))
				.then(function (response) {
					return response.json().then(function (data) {
						if (!response.ok) {
							throw new Error(data.error || labels.feedFailed);
						}
						return data;
					});
				})
				.then(function (data) {
					feedStatus.hidden = true;
					state.feed_url = data.feed_url;
					state.show_title = data.title || data.feed_url;
					state.show_image = data.image_url || null;
					if (!keepSelection) {
						state.episodes = [];
					}
					loadedEpisodes = data.episodes || [];
					renderShow();
					save();
					updateVisibility();
					renderEpisodes();
				})
				.catch(function (error) {
					setMessage(feedStatus, error.message || labels.feedFailed, true);
				});
		}

		function renderShow() {
			showRow.hidden = false;
			showEl.innerHTML = "";
			if (state.show_image) {
				var image = document.createElement("img");
				image.className = "podcast-cover";
				image.src = state.show_image;
				image.alt = "";
				image.width = 48;
				image.height = 48;
				showEl.appendChild(image);
			}
			var main = text("div", "podcast-show-main");
			main.appendChild(text("strong", null, state.show_title));
			var meta = showMeta({
				episode_count: loadedEpisodes.length,
				last_item_added: loadedEpisodes.length ? loadedEpisodes[0].publish_date : null
			});
			if (meta) {
				main.appendChild(text("span", "podcast-meta", meta));
			}
			showEl.appendChild(main);

			var change = text("button", "btn btn-small btn-secondary", labels.change);
			change.type = "button";
			change.addEventListener("click", function () {
				feedInput.focus();
				feedInput.select();
			});
			showEl.appendChild(change);
		}

		// -- episodes ----------------------------------------------------
		function isChosen(episodeId) {
			return state.episodes.some(function (episode) {
				return episode.id === String(episodeId);
			});
		}

		function toggleEpisode(episode, checked) {
			var entry = {
				id: String(episode.id),
				title: episode.title || "",
				publish_date: episode.publish_date || null,
				duration: episode.duration || 0
			};
			if (!checked) {
				state.episodes = state.episodes.filter(function (chosen) {
					return chosen.id !== entry.id;
				});
			} else if (isSingleFileMode()) {
				// Behaves like a radio group in the single-track modes, mirroring
				// what the library browser does for files.
				state.episodes = [entry];
			} else if (state.episodes.length >= config.maxEpisodes) {
				return false;
			} else if (!isChosen(entry.id)) {
				state.episodes.push(entry);
			}
			save();
			renderEpisodes();
			return true;
		}

		// How many of the listed episodes "always the newest" currently covers.
		function previewCount() {
			var wanted = isSingleFileMode() ? 1 : state.episode_count || 1;
			return Math.min(wanted, loadedEpisodes.length);
		}

		// Clicking a row while previewing: the short way from "actually, just
		// that one" to a fixed selection.
		function pickOnly(episode) {
			state.selection = "episodes";
			state.episodes = [];
			episodesRadio.checked = true;
			toggleEpisode(episode, true);
			updateVisibility();
		}

		// A row carries a checkbox only while picking. In "always the newest"
		// the list is a preview, and a disabled, empty checkbox would read as
		// "nothing selected" — exactly the opposite of what the marker says.
		function episodeRow(episode, index, picking, marked) {
			var row;
			if (picking) {
				row = text("label", "podcast-episode");
				var checkbox = document.createElement("input");
				checkbox.type = "checkbox";
				checkbox.checked = isChosen(episode.id);
				checkbox.addEventListener("change", function () {
					if (!toggleEpisode(episode, checkbox.checked)) {
						checkbox.checked = false;
					}
				});
				row.appendChild(checkbox);
			} else {
				row = text("button", "podcast-episode");
				// Inside a form an untyped button submits it.
				row.type = "button";
				row.addEventListener("click", function () {
					pickOnly(episode);
				});
				// The badge slot is always there, empty on the rows below the
				// window — otherwise the titles sit on a ragged left edge.
				var newest = index < marked;
				row.classList.add(newest ? "is-newest" : "is-dimmed");
				row.appendChild(text("span", "podcast-badge", newest ? labels.newest : ""));
			}

			var main = text("span", "podcast-episode-main");
			main.appendChild(text("span", "podcast-episode-title", episode.title));
			var meta = [
				formatDate(episode.publish_date),
				formatDuration(episode.duration),
				formatSize(episode.size)
			]
				.filter(Boolean)
				.join(" · ");
			main.appendChild(text("span", "podcast-meta", meta));
			row.appendChild(main);
			return row;
		}

		function renderEpisodes() {
			var picking = state.selection === "episodes";
			episodesNote.textContent = picking ? labels.pickNote : labels.previewNote;
			if (!loadedEpisodes.length) {
				setMessage(episodesEl, labels.noEpisodes, false);
				return;
			}
			episodesEl.hidden = false;
			episodesEl.innerHTML = "";
			var marked = picking ? 0 : previewCount();
			loadedEpisodes.forEach(function (episode, index) {
				episodesEl.appendChild(episodeRow(episode, index, picking, marked));
			});
		}

		// -- wiring ------------------------------------------------------
		function updateVisibility() {
			var hasFeed = !!state.feed_url;
			showRow.hidden = !hasFeed;
			selectionRow.hidden = !hasFeed;
			playModeRow.hidden = !hasFeed;
			countRow.hidden = state.selection !== "latest";
			// The list stays up in both modes — seeing what the feed holds is
			// just as useful when the hub does the picking.
			episodesEl.hidden = !hasFeed;
			episodesNote.hidden = !hasFeed;
			countInput.disabled = isSingleFileMode();
			if (isSingleFileMode()) {
				countInput.value = 1;
				state.episode_count = 1;
				if (state.episodes.length > 1) {
					state.episodes = state.episodes.slice(0, 1);
				}
				countRow.title = labels.singleEpisodeOnly;
			} else {
				countRow.title = "";
			}
			save();
		}

		feedBtn.addEventListener("click", function () {
			loadFeed(false);
		});
		feedInput.addEventListener("keydown", function (event) {
			if (event.key === "Enter") {
				// Enter in the feed box must load the feed, not submit the
				// half-filled assignment form.
				event.preventDefault();
				loadFeed(false);
			}
		});

		[latestRadio, episodesRadio].forEach(function (radio) {
			radio.addEventListener("change", function () {
				state.selection = radio.value;
				save();
				updateVisibility();
				renderEpisodes();
			});
		});

		countInput.addEventListener("input", function () {
			var value = parseInt(countInput.value, 10);
			if (isNaN(value) || value < 1) {
				value = 1;
			}
			state.episode_count = Math.min(value, config.maxEpisodes);
			save();
			// Live feedback for the number field: re-mark the preview.
			if (state.selection === "latest") {
				renderEpisodes();
			}
		});

		playModeSelect.addEventListener("change", function () {
			// A single-file mode caps the card at one episode, which changes
			// both the ticks and how much of the preview is marked.
			updateVisibility();
			renderEpisodes();
		});

		// Re-open an existing podcast card with its saved intent in place.
		if (config.initial && config.initial.feed_url) {
			var initial = config.initial;
			state.feed_url = initial.feed_url;
			state.show_title = initial.show_title || initial.feed_url;
			state.show_image = initial.show_image || null;
			state.selection = initial.selection === "episodes" ? "episodes" : "latest";
			state.episode_count = initial.episode_count || 1;
			state.episodes = (initial.episodes || []).map(function (episode) {
				return {
					id: String(episode.id),
					title: episode.title || "",
					publish_date: episode.publish_date || null,
					duration: episode.duration || 0
				};
			});
			countInput.value = state.episode_count;
			feedInput.value = state.feed_url;
			renderShow();
			// Refetch so the episode list is on screen (and tickable) right
			// away — the card only stores what was picked, not the feed.
			loadFeed(true);
		}

		(state.selection === "episodes" ? episodesRadio : latestRadio).checked = true;
		updateVisibility();
	}

	document.addEventListener("DOMContentLoaded", function () {
		var section = document.getElementById("mh-podcast-section");
		if (section) {
			initPodcastPicker(section);
		}
	});
})();
