{
  lib,
  python3,
  stdenv,
  makeWrapper,
}:
# The project has no pyproject.toml / setup.py — it is a Flask app served by
# gunicorn (`gunicorn app:app`), so we assemble the runtime environment
# directly instead of buildPythonPackage. This mirrors the Docker image:
# source tree + deps on PYTHONPATH, data/media dirs passed in via env vars
# at run time.
let
  pythonEnv = python3.withPackages (ps: [
    ps.flask
    ps.flask-babel
    ps.gunicorn
    # Cover art out of the media library's own tags (cover_art.py). Pure Python,
    # so the Docker image's arm/v7 build is unaffected.
    ps.mutagen
  ]);
in
  stdenv.mkDerivation {
    pname = "espuino-mediahub";
    version = "0.1.0";
    # data/ (db.json, secret.key) and media/ (the admin's own audio library)
    # are runtime state, not code — exclude them from the store copy.
    # cleanSource honors .gitignore (drops __pycache__, *.pyc, compiled *.mo).
    src = lib.cleanSourceWith {
      src = ./..;
      filter = path: type:
        baseNameOf path
        != "data"
        && baseNameOf path != "media"
        && lib.cleanSourceFilter path type;
    };
    nativeBuildInputs = [makeWrapper pythonEnv];
    # Reused by the flake's devShell so the dependency list lives in one place.
    passthru.pythonEnv = pythonEnv;
    # Compile the .po catalogs to .mo at build time, exactly like the Dockerfile
    # does (`pybabel compile -d translations`).
    buildPhase = ''
      runHook preBuild
      pybabel compile -d translations
      runHook postBuild
    '';
    installPhase = ''
      runHook preInstall
      mkdir -p $out/share/espuino-mediahub
      cp -r \
        app.py cover_art.py espuino_client.py manifest.py media_library.py \
        print_back.py printer_client.py store.py \
        templates static translations babel.cfg messages.pot \
        $out/share/espuino-mediahub/
      mkdir -p $out/bin
      makeWrapper ${pythonEnv}/bin/gunicorn $out/bin/espuino-mediahub \
        --add-flags "--chdir $out/share/espuino-mediahub app:app"
      runHook postInstall
    '';
    meta = {
      description = "Lightweight local hub for centrally managing the RFID assignments of multiple ESPuinos";
      mainProgram = "espuino-mediahub";
    };
  }
