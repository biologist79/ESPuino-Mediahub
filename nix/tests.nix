# Tests for the ESPuino MediaHub NixOS module (nix/module.nix).
#
# Two kinds of checks:
#   - eval-*  : fast, no VM — evaluate a minimal nixosSystem with the module
#               and grep the rendered systemd unit + user/group assertions.
#   - integration: full runNixOSTest VM that boots the service and curls it.
{
  lib,
  pkgs,
  nixpkgs,
  module,
}: let
  mkSystem = extra:
    nixpkgs.lib.nixosSystem {
      system = pkgs.system;
      modules = [
        module
        {
          system.stateVersion = "24.11";
          boot.loader.grub.device = "/dev/sda";
          fileSystems."/" = {
            device = "/dev/sda1";
            fsType = "ext4";
          };
        }
        extra
      ];
    };

  unitText = sys: sys.config.systemd.units."espuino-mediahub.service".text;

  # A check that the rendered unit contains all the given fragments, and
  # none of the given "absent" fragments (for directives an empty option
  # value drops entirely, e.g. CapabilityBoundingSet=[] renders nothing).
  grepCheck = name: needles: absent: unit:
    pkgs.runCommand "mediahub-check-${name}" {
      inherit unit;
      passAsFile = ["unit"];
    } (
      lib.concatStrings (map (n: ''
          grep -qF -- ${lib.escapeShellArg n} "$unitPath" \
            || { echo "FAIL ${name}: unit missing: ${lib.escapeShellArg n}"; exit 1; }
        '')
        needles)
      + lib.concatStrings (map (n: ''
          if grep -qF -- ${lib.escapeShellArg n} "$unitPath"; then
            echo "FAIL ${name}: unit should NOT contain: ${lib.escapeShellArg n}"; exit 1;
          fi
        '')
        absent)
      + ''
        touch $out
      ''
    );

  evalChecks = let
    sys = mkSystem {services.espuino-mediahub.enable = true;};
  in {
    eval-defaults = grepCheck "defaults" [
      "--bind 0.0.0.0:8080"
      "--workers 2"
      "--timeout 600"
      "--access-logfile -"
      "WorkingDirectory=/var/lib/espuino-mediahub"
      "StateDirectory=espuino-mediahub"
      "StateDirectoryMode=0750"
      "RuntimeDirectory=espuino-mediahub"
      "RuntimeDirectoryMode=0750"
      "Environment=MEDIAHUB_DATA=/var/lib/espuino-mediahub/data"
      "Environment=MEDIAHUB_MEDIA=/media"
      "Environment=XDG_RUNTIME_DIR=/run/espuino-mediahub"
      "User=mediahub"
      "Group=mediahub"
      "RequiresMountsFor=/media"
      "Restart=on-failure"
      "PrivateTmp=true"
      "ProtectSystem=strict"
      "ReadWritePaths=/var/lib/espuino-mediahub"
      "NoNewPrivileges=true"
      "ProtectHome=true"
      "ProtectKernelTunables=true"
      "ProtectKernelModules=true"
      "ProtectKernelLogs=true"
      "ProtectControlGroups=true"
      "ProtectClock=true"
      "ProtectHostname=true"
      "LockPersonality=true"
      "RestrictSUIDSGID=true"
      "RestrictRealtime=true"
      "RestrictNamespaces=true"
      "RestrictAddressFamilies=AF_INET"
      "RestrictAddressFamilies=AF_INET6"
      "RestrictAddressFamilies=AF_UNIX"
      "SystemCallArchitectures=native"
      "PrivateDevices=true"
      "ProtectProc=invisible"
      "ProcSubset=pid"
      "UMask=0077"
      "CapabilityBoundingSet="
    ] [
      # Unset by default, and then it is not in the unit at all — an empty
      # MEDIAHUB_PRINTER_URL and an absent one mean the same thing.
      "MEDIAHUB_PRINTER_URL"
    ] (unitText sys);

    eval-overrides = let
      sys' = mkSystem {
        services.espuino-mediahub.enable = true;
        services.espuino-mediahub.port = 9090;
        services.espuino-mediahub.mediaDir = "/mnt/audiobooks";
        services.espuino-mediahub.printerUrl = "http://printer.local:8081";
        services.espuino-mediahub.gunicornArgs = ["--workers 4" "--timeout 300"];
      };
    in
      grepCheck "overrides" [
        "--bind 0.0.0.0:9090"
        "Environment=MEDIAHUB_MEDIA=/mnt/audiobooks"
        "Environment=MEDIAHUB_PRINTER_URL=http://printer.local:8081"
        "--workers 4"
        "--timeout 300"
      ] [] (unitText sys');

    eval-firewall = let
      sysFw = mkSystem {
        services.espuino-mediahub.enable = true;
        services.espuino-mediahub.openFirewall = true;
      };
    in
      assert lib.elem 8080 sysFw.config.networking.firewall.allowedTCPPorts;
      assert !(lib.elem 8080 sys.config.networking.firewall.allowedTCPPorts);
        pkgs.runCommand "mediahub-check-firewall" {} ''
          touch $out
        '';

    eval-user-created = assert sys.config.users.users.mediahub.isSystemUser;
    assert sys.config.users.groups ? mediahub;
    assert sys.config.users.users.mediahub.group == "mediahub";
      pkgs.runCommand "mediahub-check-user-created" {} ''
        touch $out
      '';

    eval-disabled = let
      sysOff = mkSystem {services.espuino-mediahub.enable = false;};
    in
      assert !(sysOff.config.systemd.services ? espuino-mediahub);
      assert !(sysOff.config.users.users ? mediahub);
        pkgs.runCommand "mediahub-check-disabled" {} ''
          touch $out
        '';
  };

  integration = pkgs.testers.runNixOSTest {
    name = "espuino-mediahub-integration";
    nodes.machine = {
      imports = [module];
      services.espuino-mediahub.enable = true;
      # The app lazily reads MEDIAHUB_MEDIA, but the browser needs the dir to
      # exist — create it like a real library mount would.
      systemd.tmpfiles.rules = ["d /media 0755 root root -"];
    };

    testScript = ''
      machine.wait_for_unit("espuino-mediahub.service")
      machine.wait_for_open_port(8080)

      out = machine.succeed("curl -s http://localhost:8080/health")
      # jsonify emits compact JSON — no space after the colon.
      assert '"status":"ok"' in out, f"health: {out}"

      out = machine.succeed(
          "curl -s http://localhost:8080/ | grep -o '<title>[^<]*</title>'")
      assert "ESPuino MediaHub" in out, f"index: {out}"

      # The app created its state files inside the StateDirectory.
      machine.succeed("test -f /var/lib/espuino-mediahub/data/db.json")
      machine.succeed("test -f /var/lib/espuino-mediahub/data/secret.key")

      # Unknown card -> pending registration, HTTP 404.
      code = machine.succeed(
          "curl -s -o /dev/null -w '%{http_code}' "
          "http://localhost:8080/esp1/card/123456789012/manifest.json")
      assert code == "404", f"pending manifest: {code}"
    '';
  };
in {
  inherit (evalChecks) eval-defaults eval-overrides eval-firewall eval-user-created eval-disabled;
  inherit integration;
}
