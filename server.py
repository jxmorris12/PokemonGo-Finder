"""
The server is started with the command:
server.py interface-ip/port (e.g.: 127.0.0.1/12345)

Warning: this script is only usable for test purposes with trusted clients since
it does not address authentication aspects and does not prevent DoS attacks.
Queues are kept in memory and are lost when the server is shutdown.


The exposed RESTful calls are the following (for each call we specify the list
of supported fields sent using the urlencoding scheme in the body of the
request); results are returned using a JSON dictionary and successful requests
return the status code 200:
    - GET /
        - Required fields:
            - id:  identity of the request
            - lat: latitude
            - lng: longitude
            - rad: radius of research
        - Example:
            http://127.0.0.1:12345/?id=5&lat=45.2156&lng=2.4586&rad=4
        - Result of the request:
                {
                    "id": idOfTheRequest,
                    "pokemons": "[{
                                      "id": idOfThePokemon,
                                      "lat": latitude,
                                      "lng": longitude
                                  }, ... ]",
                    "gyms": "[{
                                  "team": numberOfTheTeam,
                                  "lat": latitude,
                                  "lng": longitude,
                                  "score": score
                              }, ... ]",
                    "pokestop": "[{
                                      "lat": latitude,
                                      "lng": longitude,
                                      "lured": lured,
                                      "expire_time": expireTime
                                  }, ...]"
                }
"""

import logging
import os

import main
import cgi
import sys
import json
from threading import Lock
from http.server import BaseHTTPRequestHandler, HTTPServer
from httplib import HTTPException
from socketserver import ThreadingMixIn


def find_pokemons(location, steplimit, x, y):
    pokemons = []
    gyms = []
    pokestops = []

    args = main.get_args()

    main.retrying_set_location(location)

    api_endpoint, access_token, profile_response = main.login(args)

    main.clear_stale_pokemons()

    # Scan location math
    if -steplimit / 2 < x <= steplimit / 2 \
            and -steplimit / 2 < y <= steplimit / 2:
        main.set_location_coords(x * 0.0025 + main.origin_lat,
                                 y * 0.0025 + main.origin_lon, 0)

    origin = main.LatLng.from_degrees(main.FLOAT_LAT, main.FLOAT_LONG)
    step_lat = main.FLOAT_LAT
    step_long = main.FLOAT_LONG
    parent = main.CellId.from_lat_lng(origin).parent(15)
    h = main.get_heartbeat(args.auth_service,
                           api_endpoint,
                           access_token,
                           profile_response)
    hs = [h]
    seen = set([])

    for child in parent.children():
        latlng = main.LatLng.from_point(main.Cell(child).get_center())
        main.set_location_coords(latlng.lat().degrees, latlng.lng().degrees, 0)
        hs.append(main.get_heartbeat(
                args.auth_service,
                api_endpoint,
                access_token,
                profile_response))
    main.set_location_coords(step_lat, step_long, 0)
    visible = []

    for hh in hs:
        try:
            for cell in hh.cells:
                for wild in cell.WildPokemon:
                    _hash = wild.SpawnPointId + ':' \
                            + str(wild.pokemon.PokemonId)
                    if _hash not in seen:
                        visible.append(wild)
                        seen.add(_hash)
                if cell.Fort:
                    for Fort in cell.Fort:
                        if Fort.Enabled:
                            if Fort.GymPoints:
                                gyms.append(Gym(Fort.Team,
                                                Fort.Latitude,
                                                Fort.Longitude,
                                                Fort.GymPoints))

                            elif Fort.FortType:
                                expire_time = 0
                                if Fort.LureInfo.LureExpiresTimestampMs:
                                    expire_time = \
                                        main.datetime.fromtimestamp(
                                                Fort.LureInfo
                                                .LureExpiresTimestampMs / 1000.0
                                        ).strftime("%H:%M:%S")
                                pokestops.append(PokeStop(Fort.Latitude,
                                                          Fort.Longitude,
                                                          expire_time > 0,
                                                          expire_time))
        except AttributeError:
            break

    for poke in visible:
        disappear_timestamp = main.time.time() + poke.TimeTillHiddenMs / 1000

        pokemons.append(Pokemon(poke.pokemon.PokemonId,
                                poke.Latitude,
                                poke.Longitude,
                                main.datetime.fromtimestamp(disappear_timestamp)
                                .strftime("%H:%M:%S"),
                                long(disappear_timestamp),
                                poke.SpawnPointId))

    return pokemons, gyms, pokestops


class PokeStop(object):
    def __init__(self, lat, lng, lured, expire_time):
        self.lat = lat
        self.lng = lng
        self.lured = lured
        self.expire_time = expire_time

    def to_json(self):
        return {
            "lat": self.lat,
            "lng": self.lng,
            "lured": self.lured,
            "expire_time": self.expire_time}


class Gym(object):
    def __init__(self, team, lat, lng, score):
        self.team = team
        self.lat = lat
        self.lng = lng
        self.score = score

    def to_json(self):
        return {
            "team": self.team,
            "lat": self.lat,
            "lng": self.lng,
            "score": self.score}


class Pokemon(object):
    def __init__(self, number, lat, lng, expire_time, disappear_time, spawn_id):
        self.number = number
        self.lng = lng
        self.lat = lat
        self.expire_time = expire_time
        self.disappear_time = disappear_time
        self.spawn_id = spawn_id

        self._hash = hash(str(spawn_id) + ':' + str(number))

    def to_json(self):
        return {
            "id": self.number,
            "lat": self.lat,
            "lng": self.lng,
            "expire_time": self.expire_time,
            "disappear_time": self.disappear_time,
            "hash": self._hash}

    def __hash__(self):
        return self._hash

    def __eq__(self, other):
        return self.number == other.number\
               and self.spawn_id == other.spawn_id


class PokemonHandlerFactory(object):
    def __init__(self):
        self.lock = Lock()

    def treat_request(self, request):
        from urlparse import urlparse
        result = None

        url = urlparse(request.path)
        env = {"REQUEST_METHOD": request.command, "QUERY_STRING": url.query,
               "CONTENT_LENGTH": request.headers.get('Content-Length', -1),
               "CONTENT_TYPE": request.headers.get('Content-Type', None)}
        parsed = cgi.parse(request.rfile, env)

        def get_field(name, integer=False, double=False):
            r = parsed.get(name)
            if not r:
                return None
            if integer:
                return int(r[0])
            if double:
                return float(r[0])
            return r[0]

        try:
            if request.command == "GET":
                idy = get_field("id", integer=True)
                lat = get_field("lat", double=True)
                lng = get_field("lng", double=True)
                rad = get_field("rad", integer=True)
                x = get_field("x", integer=True)
                y = get_field("y", integer=True)
                if not idy or not lat or not lng or not rad:
                    raise HTTPException(417, "All the fields were not supplied")

                pokemons, gyms, pokestops =\
                    find_pokemons(str(lat) + ", " + str(lng), rad, x, y)

                if len(pokemons) == 0:
                    main.login_session = None
                    pokemons, gyms, pokestops = \
                        find_pokemons(str(lat) + ", " + str(lng), rad)

                result = {"id": idy,
                          "pokemons": [p.to_json() for p in pokemons],
                          "gyms": [g.to_json() for g in gyms],
                          "pokestops": [s.to_json() for s in pokestops]}

            elif request.command == "POST":
                r2 = json.dumps({"dump": "ok"}).encode("UTF-8")
                request.send_response(200, 'OK')
                request.send_header('Content-Type', 'application/json')
                request.send_header('Content-Length', str(len(r2)))
                request.end_headers()
                request.wfile.write(r2)
                restart()

        except HTTPException as e:
            request.send_response(417, e.message)
            request.end_headers()
        except Exception as e:
            request.send_response(
                    500,
                    "An exception was encountered with the message {}".format(e)
            )
            request.end_headers()
        else:
            r2 = json.dumps(result).encode("UTF-8")
            request.send_response(200, 'OK')
            request.send_header('Content-Type', 'application/json')
            request.send_header('Content-Length', str(len(r2)))
            request.end_headers()
            request.wfile.write(r2)

    def get_handler(self):
        p = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): return p.treat_request(self)

            def do_POST(self): return p.treat_request(self)

        return Handler


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    pass


def restart():
    prog = sys.executable
    os.execl(prog, prog, *sys.argv)


if __name__ == '__main__':
    logger = logging.getLogger("PokemonGo-Finder")
    logger.setLevel(logging.INFO)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(-1)
    else:
        (iface, port) = sys.argv[1].split('/', 1)
        httpServer = ThreadingHTTPServer((iface, int(port)),
                                         PokemonHandlerFactory().get_handler())
        httpServer.serve_forever()
