{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.espuino-mediahub;
in {
  options.services.espuino-mediahub = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to enable the ESPuino MediaHub web service.";
    };
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix {};
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix {}";
      description = "The ESPuino MediaHub package to run.";
    };
    mediaDir = lib.mkOption {
      type = lib.types.str;
      default = "/media";
      description = ''
        Root of the audio library MediaHub browses and serves. Must already
        exist and be readable by the service user; MediaHub never writes to
        it. Matches the Docker default MEDIAHUB_MEDIA=/media — on NixOS
        point this at your actual library, e.g. "/mnt/audiobooks".
      '';
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 8080;
      description = "TCP port the hub listens on.";
    };
    user = lib.mkOption {
      type = lib.types.str;
      default = "mediahub";
      description = ''
        User the service runs as. The module creates this user (a system
        account) when the service is enabled.
      '';
    };
    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Open the configured port in the firewall. Disabled by default since
        the hub may be deliberately reachable only on a trusted LAN — enable
        when the ESPuinos must reach it through the host firewall.
      '';
    };
    printerUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        URL of the card-printing service (ESPuino label-printer) this hub places
        print orders at. It is used both for the hub's own API calls and for the
        link a customer follows to pay, so it must be reachable from this host
        *and* from the browser — a host/IP:port, not a container or service name.
        Unset means the print-shop integration is off.
      '';
      example = "http://printer.local:8081";
    };
    gunicornArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [
        "--workers 2"
        "--timeout 600"
        "--access-logfile -"
      ];
      description = ''
        Extra gunicorn arguments, appended to the ExecStart command. The
        defaults mirror the Docker image's CMD: 2 workers, a 600s timeout
        (the 30s default kills slow WiFi file downloads — see the Dockerfile
        comment) and access logs on stdout.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.user;
    };
    users.groups.${cfg.user} = {};

    networking.firewall = lib.mkIf cfg.openFirewall {
      allowedTCPPorts = [cfg.port];
    };

    systemd.services.espuino-mediahub = {
      description = "ESPuino MediaHub — RFID assignment hub for ESPuinos";
      wantedBy = ["multi-user.target"];
      after = ["network.target" "network-online.target"];
      wants = ["network-online.target"];
      unitConfig.RequiresMountsFor = cfg.mediaDir;
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.user;
        # systemd creates and owns /var/lib/espuino-mediahub (0750); the app
        # creates the "data" subdir itself (store.py makedirs).
        WorkingDirectory = "/var/lib/espuino-mediahub";
        StateDirectory = "espuino-mediahub";
        StateDirectoryMode = "0750";
        # gunicorn 26's control server puts its Unix socket in
        # $XDG_RUNTIME_DIR/gunicorn.ctl, falling back to $HOME/.gunicorn/.
        # The service user has no writable $HOME (and HOME is /var/empty,
        # read-only under ProtectSystem=strict), so give it a real runtime
        # dir — systemd creates /run/espuino-mediahub owned by the service
        # user and exports it as XDG_RUNTIME_DIR.
        RuntimeDirectory = "espuino-mediahub";
        RuntimeDirectoryMode = "0750";
        Environment = [
          "MEDIAHUB_DATA=/var/lib/espuino-mediahub/data"
          "MEDIAHUB_MEDIA=${cfg.mediaDir}"
          # systemd creates /run/espuino-mediahub (RuntimeDirectory=, owned by
          # the service user) and exports it here explicitly so gunicorn's
          # control socket lands there instead of $HOME/.gunicorn (read-only
          # under ProtectSystem=strict).
          "XDG_RUNTIME_DIR=/run/espuino-mediahub"
        ]
        # Only when set: an empty MEDIAHUB_PRINTER_URL and an absent one mean the
        # same thing, and leaving it out keeps the unit honest about that.
        ++ lib.optional (cfg.printerUrl != null) "MEDIAHUB_PRINTER_URL=${cfg.printerUrl}";
        ExecStart =
          "${cfg.package}/bin/espuino-mediahub --bind 0.0.0.0:${toString cfg.port} "
          + lib.concatStringsSep " " cfg.gunicornArgs;
        Restart = "on-failure";
        RestartSec = 2;
        # Hardening
        PrivateTmp = true;
        ProtectSystem = "strict";
        # MediaHub never writes to the library, so mediaDir stays read-only
        # under ProtectSystem=strict (only the state dir is writable).
        ReadWritePaths = ["/var/lib/espuino-mediahub"];
        NoNewPrivileges = true;
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        LockPersonality = true;
        RestrictSUIDSGID = true;
        RestrictRealtime = true;
        RestrictNamespaces = true;
        # The hub only needs TCP (bind/listen + outbound to ESPuinos for
        # secure delete) and AF_UNIX (gunicorn's control socket).
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        SystemCallArchitectures = "native";
        PrivateDevices = true;
        ProtectProc = "invisible";
        ProcSubset = "pid";
        # db.json holds the password hash and secret.key — keep new files
        # out of group/other hands. CapabilityBoundingSet=[ "" ] renders the
        # empty directive, dropping ALL capabilities (an empty *list* would
        # render nothing and silently keep the defaults).
        UMask = "0077";
        CapabilityBoundingSet = [""];
      };
    };
  };
}
