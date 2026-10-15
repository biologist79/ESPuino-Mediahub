// Inline media-library tree browser for the card assignment form.
// Mirrors the ESPuino web UI's own SD file explorer (lazy-loading tree,
// folders before files, click-to-select) but hand-written in vanilla JS
// to match this project's no-framework/offline-first frontend.
(function () {
	"use strict";

	function initMediaBrowser(root) {
		var listUrl = root.dataset.listUrl;
		var recursiveListUrl = root.dataset.recursiveListUrl;
		var labels = JSON.parse(document.getElementById("media-tree-labels").textContent);
		// Read from the JSON <script> block, not an HTML attribute: a raw
		// tojson'd path containing a `"` would otherwise terminate the
		// value="..." attribute early and truncate/corrupt it.
		var selection = (labels.initialSelection || []).slice();
		var singleFileModes = (labels.singleFileModes || []).map(String);
		var recursivePlayModes = (labels.recursivePlayModes || []).map(String);
		var singleFolderModes = (labels.singleFolderModes || []).map(String);
		var gatherRecursiveModes = (labels.gatherRecursiveModes || []).map(String);
		var playModeSelect = document.getElementById("play_mode");
		var playRootInput = document.getElementById("play-root");

		var selectedListEl = document.getElementById("selected-files");
		var selectedEmptyEl = document.getElementById("selected-files-empty");
		var folderWarningEl = document.getElementById("selected-files-warning");
		var clearSelectionBtn = document.getElementById("clear-selection");
		var selectedCountEl = document.getElementById("selected-count");
		var hiddenInput = document.getElementById("selected-paths-json");

		function isSingleFileMode() {
			return !!playModeSelect && singleFileModes.indexOf(playModeSelect.value) !== -1;
		}

		function isRecursiveMode() {
			return !!playModeSelect && recursivePlayModes.indexOf(playModeSelect.value) !== -1;
		}

		// These play modes hand the ESPuino one folder to scan, not a file list,
		// so everything selected has to sit in the same one — see
		// SINGLE_FOLDER_PLAY_MODES in manifest.py.
		// Whether "use folder" has to reach into subfolders. Not the same as
		// isRecursiveMode(): "random subfolder" does not descend at playback, it
		// picks one subfolder and plays that - but those subfolders have to be
		// in the manifest for it to have anything to pick from.
		function gathersRecursively() {
			return !!playModeSelect && gatherRecursiveModes.indexOf(playModeSelect.value) !== -1;
		}

		// The folder "use folder" was applied to. It travels to the ESPuino,
		// which plays a folder rather than a file list - without it the device
		// has to guess the folder back from the paths, and a folder holding a
		// single subfolder makes that guess land one level too deep. Cleared as
		// soon as files are picked by hand, because then no single folder is
		// behind the selection any more.
		function setPlayRoot(path) {
			if (playRootInput) {
				playRootInput.value = path || "";
			}
		}

		function isSingleFolderMode() {
			return !!playModeSelect && singleFolderModes.indexOf(playModeSelect.value) !== -1;
		}

		function folderOf(path) {
			var cut = path.lastIndexOf("/");
			return cut === -1 ? "" : path.substring(0, cut);
		}

		function selectedFolders() {
			var folders = [];
			selection.forEach(function (path) {
				var dir = folderOf(path);
				if (folders.indexOf(dir) === -1) {
					folders.push(dir);
				}
			});
			return folders;
		}

		// `blocked` is set right after an attempt to add a file from a second
		// folder; the spread-selection warning outranks it, since that one is a
		// standing problem and not a single refused click.
		function updateFolderWarning(blocked) {
			if (!folderWarningEl) {
				return;
			}
			var text = "";
			if (isSingleFolderMode()) {
				if (selectedFolders().length > 1) {
					text = labels.singleFolderConflict;
				} else if (blocked) {
					text = labels.singleFolderBlocked;
				}
			}
			folderWarningEl.textContent = text;
			folderWarningEl.hidden = text === "";
		}

		function useFolderTitle(disabled) {
			if (disabled) {
				return labels.singleFileOnly;
			}
			return isRecursiveMode() ? labels.recursiveHint : "";
		}

		function updateUseFolderButtons() {
			var disabled = isSingleFileMode();
			root.querySelectorAll(".mt-use-btn").forEach(function (btn) {
				btn.disabled = disabled;
				btn.title = useFolderTitle(disabled);
			});
		}

		function saveSelection() {
			hiddenInput.value = JSON.stringify(selection);
			renderSelectedList();
			syncCheckboxes();
			updateFolderWarning(false);
		}

		function renderSelectedList() {
			selectedListEl.innerHTML = "";
			selectedEmptyEl.hidden = selection.length > 0;
			// Nothing to clear when nothing is selected, and "use folder" on a
			// tree of subfolders can run to a few hundred entries - unpicking
			// those one × at a time is what this button exists to avoid.
			if (clearSelectionBtn) {
				clearSelectionBtn.hidden = selection.length === 0;
			}
			// "Use folder" can bring in a few hundred files at once, and the
			// list is scrollable - the count is the only place the size of the
			// selection is visible at a glance.
			if (selectedCountEl) {
				selectedCountEl.textContent = selection.length > 0 ? " (" + selection.length + ")" : "";
			}
			selection.forEach(function (path) {
				var li = document.createElement("li");
				var nameSpan = document.createElement("span");
				nameSpan.textContent = path;
				var removeBtn = document.createElement("button");
				removeBtn.type = "button";
				removeBtn.className = "mt-remove";
				removeBtn.setAttribute("aria-label", labels.remove);
				removeBtn.textContent = "×";
				removeBtn.addEventListener("click", function () {
					setPlayRoot("");
					var idx = selection.indexOf(path);
					if (idx !== -1) {
						selection.splice(idx, 1);
						saveSelection();
					}
				});
				li.appendChild(nameSpan);
				li.appendChild(removeBtn);
				selectedListEl.appendChild(li);
			});
		}

		function syncCheckboxes() {
			root.querySelectorAll(".mt-file").forEach(function (li) {
				var checkbox = li.querySelector(".mt-checkbox");
				checkbox.checked = selection.indexOf(li.dataset.path) !== -1;
			});
		}

		function addPaths(paths) {
			if (isSingleFileMode()) {
				// Acts like a radio group in this mode: picking a new file replaces
				// whatever was selected before instead of adding to it.
				selection = paths.slice(-1);
				saveSelection();
				return;
			}
			// In a single-folder mode the first pick fixes the folder; anything
			// from elsewhere is refused here rather than at save time, so the
			// tree never shows a selection the ESPuino could not play.
			var allowedFolder = null;
			if (isSingleFolderMode()) {
				var folders = selectedFolders();
				allowedFolder = folders.length > 0 ? folders[0] : null;
			}
			var blocked = false;
			paths.forEach(function (path) {
				if (selection.indexOf(path) !== -1) {
					return;
				}
				if (isSingleFolderMode()) {
					if (allowedFolder === null) {
						allowedFolder = folderOf(path);
					} else if (folderOf(path) !== allowedFolder) {
						blocked = true;
						return;
					}
				}
				selection.push(path);
			});
			saveSelection();
			if (blocked) {
				updateFolderWarning(true);
			}
		}

		function removePath(path) {
			var idx = selection.indexOf(path);
			if (idx !== -1) {
				selection.splice(idx, 1);
				saveSelection();
			}
		}

		function joinPath(base, name) {
			return base ? base + "/" + name : name;
		}

		function fetchListing(path) {
			return fetch(listUrl + "?path=" + encodeURIComponent(path)).then(function (resp) {
				if (!resp.ok) {
					throw new Error("listing failed");
				}
				return resp.json();
			});
		}

		// Resolves to {paths, hadSubfolders}. The second half is only there so
		// an empty result can say which of the two reasons it was: a folder
		// with nothing in it, or one whose audio sits in subfolders this play
		// mode will not look into.
		function fetchFilesForUseFolder(path) {
			if (!gathersRecursively()) {
				return fetchListing(path).then(function (data) {
					return {
						paths: data.files.map(function (name) {
							return joinPath(path, name);
						}),
						hadSubfolders: data.dirs.length > 0
					};
				});
			}
			return fetch(recursiveListUrl + "?path=" + encodeURIComponent(path)).then(function (resp) {
				if (!resp.ok) {
					throw new Error("recursive listing failed");
				}
				return resp.json();
			}).then(function (data) {
				return { paths: data.files, hadSubfolders: false };
			});
		}

		function buildDirNode(path, name) {
			var li = document.createElement("li");
			li.className = "mt-dir";
			li.dataset.path = path;
			li.dataset.loaded = "false";

			var row = document.createElement("div");
			row.className = "mt-row";

			var toggle = document.createElement("span");
			toggle.className = "mt-toggle";
			toggle.textContent = "▸";

			var icon = document.createElement("span");
			icon.className = "mt-icon";
			icon.textContent = "📁";

			var nameSpan = document.createElement("span");
			nameSpan.className = "mt-name";
			nameSpan.textContent = name;

			var useBtn = document.createElement("button");
			useBtn.type = "button";
			useBtn.className = "mt-use-btn";
			useBtn.textContent = labels.useFolder;
			useBtn.disabled = isSingleFileMode();
			useBtn.title = useFolderTitle(useBtn.disabled);

			var children = document.createElement("ul");
			children.className = "mt-children";
			children.hidden = true;

			row.appendChild(toggle);
			row.appendChild(icon);
			row.appendChild(nameSpan);
			row.appendChild(useBtn);
			li.appendChild(row);
			li.appendChild(children);

			function expand() {
				if (li.dataset.loaded === "true") {
					children.hidden = !children.hidden;
					toggle.textContent = children.hidden ? "▸" : "▾";
					return;
				}
				toggle.textContent = "…";
				fetchListing(path).then(function (data) {
					renderChildren(children, data);
					li.dataset.loaded = "true";
					children.hidden = false;
					toggle.textContent = "▾";
				}).catch(function () {
					toggle.textContent = "▸";
					children.hidden = false;
					children.innerHTML = "";
					var error = document.createElement("li");
					error.className = "mt-empty mt-error";
					error.textContent = labels.listError;
					children.appendChild(error);
				});
			}

			toggle.addEventListener("click", expand);
			nameSpan.addEventListener("click", expand);

			useBtn.addEventListener("click", function () {
				var originalLabel = useBtn.textContent;
				useBtn.disabled = true;
				var startedEmpty = selection.length === 0;
				fetchFilesForUseFolder(path).then(function (result) {
					var paths = result.paths;
					// Say so instead of appearing to do nothing: the button used
					// to return silently whenever a folder held only subfolders
					// and the play mode didn't descend into them.
					if (paths.length === 0) {
						window.alert(result.hadSubfolders ? labels.useFolderSubfoldersOnly : labels.emptyFolder);
						return;
					}
					addPaths(paths);
					// Only when this folder accounts for the whole selection.
					// Adding a second folder on top leaves no root, and the
					// ESPuino falls back to deriving one from the paths - which
					// is the right answer for a selection no single folder
					// describes. Checked after adding, so a pick the mode
					// refused doesn't set a root either.
					if (startedEmpty && selection.length > 0) {
						setPlayRoot(path);
					}
				}).catch(function () {
					window.alert(labels.listError);
				}).finally(function () {
					useBtn.disabled = false;
					useBtn.textContent = originalLabel;
				});
			});

			return li;
		}

		function buildFileNode(path, name) {
			var li = document.createElement("li");
			li.className = "mt-file";
			li.dataset.path = path;

			var checkbox = document.createElement("input");
			checkbox.type = "checkbox";
			checkbox.className = "mt-checkbox";
			checkbox.checked = selection.indexOf(path) !== -1;
			checkbox.addEventListener("change", function () {
				// Ticking or unticking a single file means the selection is no
				// longer "that folder", so it stops claiming to be.
				setPlayRoot("");
				if (checkbox.checked) {
					addPaths([path]);
				} else {
					removePath(path);
				}
			});

			var icon = document.createElement("span");
			icon.className = "mt-icon";
			icon.textContent = "🎵";

			var nameSpan = document.createElement("span");
			nameSpan.className = "mt-name";
			nameSpan.textContent = name;

			li.appendChild(checkbox);
			li.appendChild(icon);
			li.appendChild(nameSpan);
			return li;
		}

		function renderChildren(container, data) {
			container.innerHTML = "";
			data.dirs.forEach(function (name) {
				container.appendChild(buildDirNode(joinPath(data.path, name), name));
			});
			data.files.forEach(function (name) {
				container.appendChild(buildFileNode(joinPath(data.path, name), name));
			});
			if (!data.dirs.length && !data.files.length) {
				var empty = document.createElement("li");
				empty.className = "mt-empty muted";
				empty.textContent = labels.emptyFolder;
				container.appendChild(empty);
			}
		}

		var tree = document.createElement("ul");
		tree.className = "mt-root";
		root.appendChild(tree);

		fetchListing("").then(function (data) {
			renderChildren(tree, data);
			updateUseFolderButtons();
		}).catch(function () {
			var error = document.createElement("li");
			error.className = "mt-empty mt-error";
			error.textContent = labels.listError;
			tree.appendChild(error);
		});

		if (clearSelectionBtn) {
			clearSelectionBtn.addEventListener("click", function () {
				selection = [];
				setPlayRoot("");
				saveSelection();
			});
		}

		if (playModeSelect) {
			playModeSelect.addEventListener("change", function () {
				if (isSingleFileMode() && selection.length > 1) {
					selection = selection.slice(0, 1);
					saveSelection();
				}
				updateUseFolderButtons();
				updateFolderWarning(false);
			});
		}

		if (isSingleFileMode() && selection.length > 1) {
			selection = selection.slice(0, 1);
		}

		// Sync the hidden field with the pre-filled selection right away —
		// otherwise clicking Save without touching the tree would submit the
		// "[]" placeholder and wipe an existing card's files.
		saveSelection();
	}

	document.addEventListener("DOMContentLoaded", function () {
		var root = document.getElementById("media-tree");
		if (root) {
			initMediaBrowser(root);
		}
	});
})();
