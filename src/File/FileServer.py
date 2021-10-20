import logging
import time
import random
import socket
import sys

import gevent
import gevent.pool
from gevent.server import StreamServer

import util
from util import helper
from Config import config
from .FileRequest import FileRequest
from Peer import PeerPortchecker
from Site import SiteManager
from Connection import ConnectionServer
from Plugin import PluginManager
from Debug import Debug

log = logging.getLogger("FileServer")

@PluginManager.acceptPlugins
class FileServer(ConnectionServer):

    def __init__(self, ip=config.fileserver_ip, port=config.fileserver_port, ip_type=config.fileserver_ip_type):
        self.site_manager = SiteManager.site_manager
        self.portchecker = PeerPortchecker.PeerPortchecker(self)
        self.ip_type = ip_type
        self.ip_external_list = []

        # This is wrong:
        # self.log = logging.getLogger("FileServer")
        # The value of self.log will be overwritten in ConnectionServer.__init__()

        self.recheck_port = True

        self.update_pool = gevent.pool.Pool(5)
        self.update_start_time = 0
        self.update_sites_task_next_nr = 1

        self.supported_ip_types = ["ipv4"]  # Outgoing ip_type support
        if helper.getIpType(ip) == "ipv6" or self.isIpv6Supported():
            self.supported_ip_types.append("ipv6")

        if ip_type == "ipv6" or (ip_type == "dual" and "ipv6" in self.supported_ip_types):
            ip = ip.replace("*", "::")
        else:
            ip = ip.replace("*", "0.0.0.0")

        if config.tor == "always":
            port = config.tor_hs_port
            config.fileserver_port = port
        elif port == 0:  # Use random port
            port_range_from, port_range_to = list(map(int, config.fileserver_port_range.split("-")))
            port = self.getRandomPort(ip, port_range_from, port_range_to)
            config.fileserver_port = port
            if not port:
                raise Exception("Can't find bindable port")
            if not config.tor == "always":
                config.saveValue("fileserver_port", port)  # Save random port value for next restart
                config.arguments.fileserver_port = port

        ConnectionServer.__init__(self, ip, port, self.handleRequest)
        log.debug("Supported IP types: %s" % self.supported_ip_types)

        self.managed_pools["update"] = self.pool

        if ip_type == "dual" and ip == "::":
            # Also bind to ipv4 addres in dual mode
            try:
                log.debug("Binding proxy to %s:%s" % ("::", self.port))
                self.stream_server_proxy = StreamServer(
                    ("0.0.0.0", self.port), self.handleIncomingConnection, spawn=self.pool, backlog=100
                )
            except Exception as err:
                log.info("StreamServer proxy create error: %s" % Debug.formatException(err))

        self.port_opened = {}

        self.last_request = time.time()
        self.files_parsing = {}
        self.ui_server = None

    def getSites(self):
        sites = self.site_manager.list()
        # We need to keep self.sites for the backward compatibility with plugins.
        # Never. Ever. Use it.
        # TODO: fix plugins
        self.sites = sites
        return sites

    def getSite(self, address):
        return self.getSites().get(address, None)

    def getSiteAddresses(self):
        # Avoid saving the site list on the stack, since a site may be deleted
        # from the original list while iterating.
        # Use the list of addresses instead.
        return [
            site.address for site in
            sorted(list(self.getSites().values()), key=lambda site: site.settings.get("modified", 0), reverse=True)
        ]

    def getRandomPort(self, ip, port_range_from, port_range_to):
        log.info("Getting random port in range %s-%s..." % (port_range_from, port_range_to))
        tried = []
        for bind_retry in range(100):
            port = random.randint(port_range_from, port_range_to)
            if port in tried:
                continue
            tried.append(port)
            sock = helper.createSocket(ip)
            try:
                sock.bind((ip, port))
                success = True
            except Exception as err:
                log.warning("Error binding to port %s: %s" % (port, err))
                success = False
            sock.close()
            if success:
                log.info("Found unused random port: %s" % port)
                return port
            else:
                self.sleep(0.1)
        return False

    def isIpv6Supported(self):
        if config.tor == "always":
            return True
        # Test if we can connect to ipv6 address
        ipv6_testip = "fcec:ae97:8902:d810:6c92:ec67:efb2:3ec5"
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.connect((ipv6_testip, 80))
            local_ipv6 = sock.getsockname()[0]
            if local_ipv6 == "::1":
                log.debug("IPv6 not supported, no local IPv6 address")
                return False
            else:
                log.debug("IPv6 supported on IP %s" % local_ipv6)
                return True
        except socket.error as err:
            log.warning("IPv6 not supported: %s" % err)
            return False
        except Exception as err:
            log.error("IPv6 check error: %s" % err)
            return False

    def listenProxy(self):
        try:
            self.stream_server_proxy.serve_forever()
        except Exception as err:
            if err.errno == 98:  # Address already in use error
                log.debug("StreamServer proxy listen error: %s" % err)
            else:
                log.info("StreamServer proxy listen error: %s" % err)

    # Handle request to fileserver
    def handleRequest(self, connection, message):
        if config.verbose:
            if "params" in message:
                log.debug(
                    "FileRequest: %s %s %s %s" %
                    (str(connection), message["cmd"], message["params"].get("site"), message["params"].get("inner_path"))
                )
            else:
                log.debug("FileRequest: %s %s" % (str(connection), message["cmd"]))
        req = FileRequest(self, connection)
        req.route(message["cmd"], message.get("req_id"), message.get("params"))
        if not connection.is_private_ip:
            self.setInternetStatus(True)

    def onInternetOnline(self):
        log.info("Internet online")
        invalid_interval=(
            self.internet_offline_since - self.internet_outage_threshold - random.randint(60 * 5, 60 * 10),
            time.time()
        )
        self.invalidateUpdateTime(invalid_interval)
        self.recheck_port = True
        self.spawn(self.updateSites)

    # Reload the FileRequest class to prevent restarts in debug mode
    def reload(self):
        global FileRequest
        import imp
        FileRequest = imp.load_source("FileRequest", "src/File/FileRequest.py").FileRequest

    def portCheck(self):
        if config.offline:
            log.info("Offline mode: port check disabled")
            res = {"ipv4": None, "ipv6": None}
            self.port_opened = res
            return res

        if config.ip_external:
            for ip_external in config.ip_external:
                SiteManager.peer_blacklist.append((ip_external, self.port))  # Add myself to peer blacklist

            ip_external_types = set([helper.getIpType(ip) for ip in config.ip_external])
            res = {
                "ipv4": "ipv4" in ip_external_types,
                "ipv6": "ipv6" in ip_external_types
            }
            self.ip_external_list = config.ip_external
            self.port_opened.update(res)
            log.info("Server port opened based on configuration ipv4: %s, ipv6: %s" % (res["ipv4"], res["ipv6"]))
            return res

        self.port_opened = {}
        if self.ui_server:
            self.ui_server.updateWebsocket()

        if "ipv6" in self.supported_ip_types:
            res_ipv6_thread = self.spawn(self.portchecker.portCheck, self.port, "ipv6")
        else:
            res_ipv6_thread = None

        res_ipv4 = self.portchecker.portCheck(self.port, "ipv4")
        if not res_ipv4["opened"] and config.tor != "always":
            if self.portchecker.portOpen(self.port):
                res_ipv4 = self.portchecker.portCheck(self.port, "ipv4")

        if res_ipv6_thread is None:
            res_ipv6 = {"ip": None, "opened": None}
        else:
            res_ipv6 = res_ipv6_thread.get()
            if res_ipv6["opened"] and not helper.getIpType(res_ipv6["ip"]) == "ipv6":
                log.info("Invalid IPv6 address from port check: %s" % res_ipv6["ip"])
                res_ipv6["opened"] = False

        self.ip_external_list = []
        for res_ip in [res_ipv4, res_ipv6]:
            if res_ip["ip"] and res_ip["ip"] not in self.ip_external_list:
                self.ip_external_list.append(res_ip["ip"])
                SiteManager.peer_blacklist.append((res_ip["ip"], self.port))

        log.info("Server port opened ipv4: %s, ipv6: %s" % (res_ipv4["opened"], res_ipv6["opened"]))

        res = {"ipv4": res_ipv4["opened"], "ipv6": res_ipv6["opened"]}

        # Add external IPs from local interfaces
        interface_ips = helper.getInterfaceIps("ipv4")
        if "ipv6" in self.supported_ip_types:
            interface_ips += helper.getInterfaceIps("ipv6")
        for ip in interface_ips:
            if not helper.isPrivateIp(ip) and ip not in self.ip_external_list:
                self.ip_external_list.append(ip)
                res[helper.getIpType(ip)] = True  # We have opened port if we have external ip
                SiteManager.peer_blacklist.append((ip, self.port))
                log.debug("External ip found on interfaces: %s" % ip)

        self.port_opened.update(res)

        if self.ui_server:
            self.ui_server.updateWebsocket()

        return res

    @util.Noparallel(queue=True)
    def recheckPort(self):
        if self.recheck_port:
            self.portCheck()
            self.recheck_port = False

    # Returns False if Internet is immediately available
    # Returns True if we've spent some time waiting for Internet
    # Returns None if FileServer is stopping or the Offline mode is enabled
    def waitForInternetOnline(self):
        if config.offline or self.stopping:
            return None

        if self.isInternetOnline():
            return False

        while not self.isInternetOnline():
            self.sleep(30)
            if config.offline or self.stopping:
                return None
            if self.isInternetOnline():
                break
            if len(self.update_pool) == 0:
                thread = self.update_pool.spawn(self.updateRandomSite)
                thread.join()

        self.recheckPort()
        return True

    def updateRandomSite(self, site_addresses=None, force=False):
        if not site_addresses:
            site_addresses = self.getSiteAddresses()

        site_addresses = random.sample(site_addresses, 1)
        if len(site_addresses) < 1:
            return

        address = site_addresses[0]
        site = self.getSite(address)

        if not site:
            return

        log.debug("Checking randomly chosen site: %s", site.address_short)

        self.updateSite(site, force=force)

    def updateSite(self, site, check_files=False, force=False, dry_run=False):
        if not site:
            return False
        return site.considerUpdate(check_files=check_files, force=force, dry_run=dry_run)

    def invalidateUpdateTime(self, invalid_interval):
        for address in self.getSiteAddresses():
            site = self.getSite(address)
            if site:
                site.invalidateUpdateTime(invalid_interval)

    def updateSites(self, check_files=False):
        self.recheckPort()

        task_nr = self.update_sites_task_next_nr
        self.update_sites_task_next_nr += 1

        task_description = "updateSites [#%d, check_files=%s]" % (task_nr, check_files)
        log.info("%s: started", task_description)

        # Don't wait port opening on first startup. Do the instant check now.
        if len(self.getSites()) <= 2:
            for address, site in list(self.getSites().items()):
                self.updateSite(site, check_files=check_files)

        all_site_addresses = self.getSiteAddresses()
        site_addresses = [
            address for address in all_site_addresses
            if self.updateSite(self.getSite(address), check_files=check_files, dry_run=True)
        ]

        log.info("%s: chosen %d sites (of %d)", task_description, len(site_addresses), len(all_site_addresses))

        sites_processed = 0
        sites_skipped = 0
        start_time = time.time()
        self.update_start_time = start_time
        progress_print_time = time.time()

        # Check sites integrity
        for site_address in site_addresses:
            if check_files:
                self.sleep(10)
            else:
                self.sleep(1)

            if self.stopping:
                break

            site = self.getSite(site_address)
            if not self.updateSite(site, check_files=check_files, dry_run=True):
                sites_skipped += 1
                continue

            sites_processed += 1

            while self.running:
                self.waitForInternetOnline()
                if self.stopping:
                    break

                thread = self.update_pool.spawn(self.updateSite, site, check_files=check_files)
                if check_files:
                    # Limit the concurency
                    # ZeroNet may be laggy when running from HDD.
                    thread.join(timeout=60)
                if not self.waitForInternetOnline():
                    break

            if self.stopping:
                break

            if time.time() - progress_print_time > 60:
                progress_print_time = time.time()
                time_spent = time.time() - start_time
                time_per_site = time_spent / float(sites_processed)
                sites_left = len(site_addresses) - sites_processed
                time_left = time_per_site * sites_left
                log.info("%s: DONE: %d sites in %.2fs (%.2fs per site); SKIPPED: %d sites; LEFT: %d sites in %.2fs",
                    task_description,
                    sites_processed,
                    time_spent,
                    time_per_site,
                    sites_skipped,
                    sites_left,
                    time_left
                )

        if self.stopping:
            log.info("%s: stopped", task_description)
        else:
            log.info("%s: finished in %.2fs", task_description, time.time() - start_time)

    def sitesMaintenanceThread(self, mode="full"):
        log.info("sitesMaintenanceThread(%s) started" % mode)

        startup = True

        short_timeout = 2
        min_long_timeout = 10
        max_long_timeout = 60 * 10
        long_timeout = min_long_timeout
        short_cycle_time_limit = 60 * 2

        while self.running:
            self.sleep(long_timeout)

            if self.stopping:
                break

            start_time = time.time()

            log.debug(
                "Starting <%s> maintenance cycle: connections=%s, internet=%s",
                mode,
                len(self.connections), self.isInternetOnline()
            )
            start_time = time.time()

            site_addresses = self.getSiteAddresses()

            sites_processed = 0

            for site_address in site_addresses:
                if self.stopping:
                    break

                site = self.getSite(site_address)
                if not site:
                    continue

                log.debug("Running maintenance for site: %s", site.address_short)

                done = site.runPeriodicMaintenance(startup=startup)
                site = None
                if done:
                    sites_processed += 1
                    self.sleep(short_timeout)

                # If we host hundreds of sites, the full maintenance cycle may take very
                # long time, especially on startup ( > 1 hour).
                # This means we are not able to run the maintenance procedure for active
                # sites frequently enough using just a single maintenance thread.
                # So we run 2 maintenance threads:
                #  * One running full cycles.
                #  * And one running short cycles for the most active sites.
                # When the short cycle runs out of the time limit, it restarts
                # from the beginning of the site list.
                if mode == "short" and time.time() - start_time > short_cycle_time_limit:
                    break

            log.debug("<%s> maintenance cycle finished in %.2fs. Total sites: %d. Processed sites: %d. Timeout: %d",
                mode,
                time.time() - start_time,
                len(site_addresses),
                sites_processed,
                long_timeout
            )

            if sites_processed:
                long_timeout = max(int(long_timeout / 2), min_long_timeout)
            else:
                long_timeout = min(long_timeout + 1, max_long_timeout)

            site_addresses = None
            startup = False
        log.info("sitesMaintenanceThread(%s) stopped" % mode)

    def keepAliveThread(self):
        # This thread is mostly useless on a system under load, since it never does
        # any works, if we have active traffic.
        #
        # We should initiate some network activity to detect the Internet outage
        # and avoid false positives. We normally have some network activity
        # initiated by various parts on the application as well as network peers.
        # So it's not a problem.
        #
        # However, if it actually happens that we have no network traffic for
        # some time (say, we host just a couple of inactive sites, and no peers
        # are interested in connecting to them), we initiate some traffic by
        # performing the update for a random site. It's way better than just
        # silly pinging a random peer for no profit.
        log.info("keepAliveThread started")
        while self.running:
            self.waitForInternetOnline()

            threshold = self.internet_outage_threshold / 2.0

            self.sleep(threshold / 2.0)
            if self.stopping:
                break

            last_activity_time = max(
                self.last_successful_internet_activity_time,
                self.last_outgoing_internet_activity_time)
            now = time.time()
            if not len(self.getSites()):
                continue
            if last_activity_time > now - threshold:
                continue
            if len(self.update_pool) != 0:
                continue

            log.info("No network activity for %.2fs. Running an update for a random site.",
                now - last_activity_time
            )
            self.update_pool.spawn(self.updateRandomSite, force=True)
        log.info("keepAliveThread stopped")

    # Periodic reloading of tracker files
    def reloadTrackerFilesThread(self):
        # TODO:
        # This should probably be more sophisticated.
        # We should check if the files have actually changed,
        # and do it more often.
        log.info("reloadTrackerFilesThread started")
        interval = 60 * 10
        while self.running:
            self.sleep(interval)
            if self.stopping:
                break
            config.loadTrackersFile()
        log.info("reloadTrackerFilesThread stopped")

    # Detects if computer back from wakeup
    def wakeupWatcherThread(self):
        log.info("wakeupWatcherThread started")
        last_time = time.time()
        last_my_ips = socket.gethostbyname_ex('')[2]
        while self.running:
            self.sleep(30)
            if self.stopping:
                break
            is_time_changed = time.time() - max(self.last_request, last_time) > 60 * 3
            if is_time_changed:
                # If taken more than 3 minute then the computer was in sleep mode
                log.info(
                    "Wakeup detected: time warp from %0.f to %0.f (%0.f sleep seconds), acting like startup..." %
                    (last_time, time.time(), time.time() - last_time)
                )

            my_ips = socket.gethostbyname_ex('')[2]
            is_ip_changed = my_ips != last_my_ips
            if is_ip_changed:
                log.info("IP change detected from %s to %s" % (last_my_ips, my_ips))

            if is_time_changed or is_ip_changed:
                invalid_interval=(
                    last_time - self.internet_outage_threshold - random.randint(60 * 5, 60 * 10),
                    time.time()
                )
                self.invalidateUpdateTime(invalid_interval)
                self.recheck_port = True
                self.spawn(self.updateSites)

            last_time = time.time()
            last_my_ips = my_ips
        log.info("wakeupWatcherThread stopped")

    # Bind and start serving sites
    # If passive_mode is False, FileServer starts the full-featured file serving:
    # * Checks for updates at startup.
    # * Checks site's integrity.
    # * Runs periodic update checks.
    # * Watches for internet being up or down and for computer to wake up and runs update checks.
    # If passive_mode is True, all the mentioned activity is disabled.
    def start(self, passive_mode=False, check_sites=None, check_connections=True):

        # Backward compatibility for a misnamed argument:
        if check_sites is not None:
            passive_mode = not check_sites

        if self.stopping:
            return False

        ConnectionServer.start(self, check_connections=check_connections)

        try:
            self.stream_server.start()
        except Exception as err:
            log.error("Error listening on: %s:%s: %s" % (self.ip, self.port, err))
            if "ui_server" in dir(sys.modules["main"]):
                log.debug("Stopping UI Server.")
                sys.modules["main"].ui_server.stop()
                return False

        if config.debug:
            # Auto reload FileRequest on change
            from Debug import DebugReloader
            DebugReloader.watcher.addCallback(self.reload)

        # XXX: for initializing self.sites
        # Remove this line when self.sites gets completely unused
        self.getSites()

        if not passive_mode:
            thread_keep_alive = self.spawn(self.keepAliveThread)
            thread_wakeup_watcher = self.spawn(self.wakeupWatcherThread)
            thread_reload_tracker_files = self.spawn(self.reloadTrackerFilesThread)
            thread_sites_maintenance_full = self.spawn(self.sitesMaintenanceThread, mode="full")
            thread_sites_maintenance_short = self.spawn(self.sitesMaintenanceThread, mode="short")

            self.sleep(0.1)
            self.spawn(self.updateSites)
            self.sleep(0.1)
            self.spawn(self.updateSites, check_files=True)

        ConnectionServer.listen(self)

        log.info("Stopped.")

    def stop(self):
        if self.running and self.portchecker.upnp_port_opened:
            log.debug('Closing port %d' % self.port)
            try:
                self.portchecker.portClose(self.port)
                log.info('Closed port via upnp.')
            except Exception as err:
                log.info("Failed at attempt to use upnp to close port: %s" % err)

        return ConnectionServer.stop(self)
