/* Cover preview on the print-card form.
 *
 * The media browser announces every selection it saves; this asks the server which
 * image that selection would print with, through the same resolution the submit
 * itself uses — so the preview cannot promise a card that creating it would then
 * refuse. An uploaded file is shown from the browser's own copy of it, and wins
 * over anything the media has.
 *
 * Only this form has both elements, so it does nothing anywhere else.
 */
(function () {
	"use strict";

	var box = document.getElementById("cover-preview");
	var upload = document.getElementById("cover");
	var tree = document.getElementById("media-tree");
	var pathsInput = document.getElementById("selected-paths-json");
	if (!box || !upload) {
		return;
	}

	var currentUrl = null;
	var sequence = 0;
	var debounceTimer = undefined;

	// The state goes on the box as well as into the text: the one state that stops
	// the card from existing (no cover anywhere) has to look like something to act
	// on, and a caption alone does not.
	function render(imageUrl, note, state) {
		box.dataset.state = state;
		box.innerHTML = "";
		if (imageUrl) {
			var img = document.createElement("img");
			img.src = imageUrl;
			img.alt = "";
			box.appendChild(img);
		}
		var caption = document.createElement("p");
		caption.className = "cover-preview-note";
		caption.textContent = note;
		box.appendChild(caption);
	}

	function replaceUrl(next) {
		if (currentUrl) {
			URL.revokeObjectURL(currentUrl);
		}
		currentUrl = next;
	}

	function showUploaded() {
		if (!upload.files || !upload.files.length) {
			return false;
		}
		replaceUrl(URL.createObjectURL(upload.files[0]));
		render(currentUrl, box.dataset.upload, "upload");
		return true;
	}

	function refreshFromMedia() {
		var raw = pathsInput ? pathsInput.value : "";
		if (!raw || raw === "[]") {
			replaceUrl(null);
			render(null, box.dataset.empty, "empty");
			return;
		}
		var mine = ++sequence;
		var body = new URLSearchParams();
		body.set("selected_paths_json", raw);
		fetch(box.dataset.coverUrl, {
			method: "POST",
			body: body,
			headers: {"content-type": "application/x-www-form-urlencoded"}
		}).then(function (response) {
			if (mine !== sequence) {
				return null;
			}
			if (!response.ok) {
				replaceUrl(null);
				render(null, box.dataset.none, "none");
				return null;
			}
			return response.blob();
		}).then(function (blob) {
			if (!blob || mine !== sequence) {
				return;
			}
			replaceUrl(URL.createObjectURL(blob));
			render(currentUrl, box.dataset.media, "media");
		}).catch(function () {
			if (mine === sequence) {
				replaceUrl(null);
				render(null, box.dataset.none, "none");
			}
		});
	}

	function refresh() {
		if (!showUploaded()) {
			refreshFromMedia();
		}
	}

	upload.addEventListener("change", refresh);
	if (tree) {
		tree.addEventListener("mh:selection-changed", function () {
			// A click on one file is one event; the fetch that each would start is
			// not free, and only the last selection matters.
			if (debounceTimer) {
				clearTimeout(debounceTimer);
			}
			debounceTimer = setTimeout(refresh, 250);
		});
	}
	refresh();
})();
