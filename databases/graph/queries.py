"""
TransitFlow — Neo4j Graph Database Layer
=========================================
This module handles all queries to Neo4j.

GRAPH ROLE:
  - Model the dual transit network (city metro M1–M4 + national rail NR1–NR2)
  - Find fastest routes (Dijkstra by travel_time_min via APOC)
  - Find cheapest routes (Dijkstra by fare via APOC)
  - Find alternative routes avoiding a given station
  - Find cross-network interchange paths (metro → rail or rail → metro)
  - Show delay ripple: which stations are affected within N hops

STUDENT TASK
------------
Design your graph schema (node labels, relationship types, properties)
based on the data in train-mock-data/, seed it with skeleton/seed_neo4j.py,
then implement the query_ functions below.

Functions prefixed with `query_` are called by the agent (skeleton/agent.py).
"""

from __future__ import annotations

from typing import Optional

from neo4j import GraphDatabase

from skeleton.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD


def _driver():
    """Return a Neo4j driver. Caller is responsible for closing."""
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


# ── Example ───────────────────────────────────────────────────────────────────
# The block below shows the query pattern: open a session, run Cypher, return data.

def example_count_nodes() -> int:
    """Example: count all nodes currently in the graph."""
    with _driver() as driver:
        with driver.session() as session:
            result = session.run("MATCH (n) RETURN count(n) AS total")
            return result.single()["total"]

# TODO: Implement the query_ functions below.
# ─────────────────────────────────────────────────────────────────────────────


# ── FASTEST ROUTE (Dijkstra by travel_time_min) ───────────────────────────────

def query_shortest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
) -> dict:
    """
    Find the fastest path between two stations, minimising total travel time.
    Uses apoc.algo.dijkstra (APOC required; enabled in docker-compose.yml).

    Args:
        origin_id:       e.g. "MS01" or "NR01"
        destination_id:  e.g. "MS09" or "NR05"
        network:         "metro", "rail", or "auto" (inferred from IDs)

    Returns:
        dict with keys: found, origin_id, destination_id,
                        total_time_min, path (list of station dicts), legs
    """
    if network == "metro":
        rel_types = "METRO_LINK>"
    elif network == "rail":
        rel_types = "RAIL_LINK>"
    else:
        rel_types = "METRO_LINK>|RAIL_LINK>|INTERCHANGE_TO>"
        
    cypher = """
        MATCH (start:Station {station_id: $origin_id})
        MATCH (end:Station {station_id: $destination_id})
        CALL apoc.algo.dijkstra(start, end, $rel_types, 'travel_time_min') YIELD path, weight
        RETURN path, weight AS total_time_min
    """
    
    with _driver() as driver:
        with driver.session() as session:
            result = session.run(cypher, origin_id=origin_id, destination_id=destination_id, rel_types=rel_types)
            record = result.single()
            if not record or not record.get("path"):
                return {"found": False, "origin_id": origin_id, "destination_id": destination_id}
                
            path = record["path"]
            total_time_min = record["total_time_min"]
            
            stations = [{"station_id": n["station_id"], "name": n["name"]} for n in path.nodes]
            legs = [{"type": r.type, "line": r.get("line", ""), "travel_time_min": r.get("travel_time_min", 0)} for r in path.relationships]
                
            return {
                "found": True,
                "origin_id": origin_id,
                "destination_id": destination_id,
                "total_time_min": total_time_min,
                "stations": stations,
                "legs": legs
            }


# ── CHEAPEST ROUTE (Dijkstra by fare) ────────────────────────────────────────

def query_cheapest_route(
    origin_id: str,
    destination_id: str,
    network: str = "auto",
    fare_class: str = "standard",
) -> dict:
    """
    Find the cheapest path between two stations, minimising total estimated fare.

    Args:
        origin_id:       e.g. "NR01"
        destination_id:  e.g. "NR05"
        network:         "metro", "rail", or "auto"
        fare_class:      "standard" or "first" (national rail only)

    Returns:
        dict with found, total_fare_usd (approximate), stations, legs
    """
    if network == "metro":
        rel_filter = "METRO_LINK"
    elif network == "rail":
        rel_filter = "RAIL_LINK"
    else:
        rel_filter = "METRO_LINK|RAIL_LINK|INTERCHANGE_TO"
        
    # Since fare is base + per-stop, the cheapest route minimizes the number of stops (hops).
    cypher = f"""
        MATCH p = shortestPath((start:Station {{station_id: $origin_id}})-[:{rel_filter}*]->(end:Station {{station_id: $destination_id}}))
        RETURN p, length(p) AS stops
    """
    with _driver() as driver:
        with driver.session() as session:
            result = session.run(cypher, origin_id=origin_id, destination_id=destination_id)
            record = result.single()
            if not record or not record.get("p"):
                return {"found": False, "origin_id": origin_id, "destination_id": destination_id}
                
            path = record["p"]
            stops = record["stops"]
            
            stations = [{"station_id": n["station_id"], "name": n["name"]} for n in path.nodes]
            legs = [{"type": r.type, "line": r.get("line", "")} for r in path.relationships]
            
            return {
                "found": True,
                "origin_id": origin_id,
                "destination_id": destination_id,
                "stops": stops,
                "stations": stations,
                "legs": legs,
                "note": "Fare is calculated per-stop. Fewest stops represents the cheapest route."
            }


# ── ALTERNATIVE ROUTES (avoiding a station) ───────────────────────────────────

def query_alternative_routes(
    origin_id: str,
    destination_id: str,
    avoid_station_id: str,
    network: str = "auto",
    max_routes: int = 3,
) -> list[list[dict]]:
    """
    Find paths between two stations that avoid a specific intermediate station.
    Useful for routing around a delayed or closed station.

    Args:
        origin_id:         e.g. "NR01"
        destination_id:    e.g. "NR05"
        avoid_station_id:  e.g. "NR03"
        network:           "metro", "rail", or "auto"
        max_routes:        max number of alternatives to return

    Returns:
        List of routes, each route is a list of leg dicts
    """
    if network == "metro":
        rel_types = "METRO_LINK"
    elif network == "rail":
        rel_types = "RAIL_LINK"
    else:
        rel_types = "METRO_LINK|RAIL_LINK|INTERCHANGE_TO"
        
    cypher = f"""
        MATCH p = (start:Station {{station_id: $origin_id}})-[:{rel_types}*1..15]->(end:Station {{station_id: $destination_id}})
        WHERE NONE(n IN nodes(p) WHERE n.station_id = $avoid_station_id)
        RETURN p, reduce(cost = 0, r IN relationships(p) | cost + coalesce(r.travel_time_min, 0)) AS total_time
        ORDER BY total_time ASC
        LIMIT $max_routes
    """
    routes = []
    with _driver() as driver:
        with driver.session() as session:
            result = session.run(cypher, origin_id=origin_id, destination_id=destination_id, avoid_station_id=avoid_station_id, max_routes=max_routes)
            for record in result:
                path = record["p"]
                total_time = record["total_time"]
                
                stations = [{"station_id": n["station_id"], "name": n["name"]} for n in path.nodes]
                routes.append({
                    "total_time_min": total_time,
                    "stations": stations
                })
    return routes


# ── CROSS-NETWORK INTERCHANGE PATH ───────────────────────────────────────────

def query_interchange_path(origin_id: str, destination_id: str) -> dict:
    """
    Find a path between a metro station and a national rail station (or vice versa)
    crossing the network boundary via interchange relationships.

    Args:
        origin_id:       e.g. "MS03" (metro) or "NR05" (national rail)
        destination_id:  e.g. "NR05" (national rail) or "MS09" (metro)

    Returns:
        dict with found, stations list, interchange points, total_time_min
    """
    # Simply use the shortest route logic with auto network which allows INTERCHANGE_TO
    result = query_shortest_route(origin_id, destination_id, network="auto")
    
    if result["found"]:
        # Find interchange points
        interchanges = []
        for leg in result["legs"]:
            if leg["type"] == "INTERCHANGE_TO":
                interchanges.append(leg)
        result["interchange_points"] = interchanges
        
    return result


# ── DELAY RIPPLE ANALYSIS ─────────────────────────────────────────────────────

def query_delay_ripple(delayed_station_id: str, hops: int = 2) -> list[dict]:
    """
    Find all stations within N hops of a delayed or disrupted station.
    Works on both metro and national rail networks.

    Args:
        delayed_station_id: e.g. "NR03" or "MS01"
        hops:               how many connections out to search (default 2)

    Returns:
        List of dicts: {station_id, name, hops_away, lines_affected}
    """
    # Using string formatting for hops bound since parameterized variable length paths aren't directly supported.
    cypher = f"""
        MATCH p = (start:Station {{station_id: $delayed_station_id}})-[:METRO_LINK|RAIL_LINK*1..{hops}]-(end:Station)
        WITH end, min(length(p)) AS hops_away
        ORDER BY hops_away ASC
        RETURN end.station_id AS station_id, end.name AS name, hops_away, end.lines AS lines_affected
    """
    affected = []
    with _driver() as driver:
        with driver.session() as session:
            result = session.run(cypher, delayed_station_id=delayed_station_id)
            for record in result:
                affected.append({
                    "station_id": record["station_id"],
                    "name": record["name"],
                    "hops_away": record["hops_away"],
                    "lines_affected": record["lines_affected"]
                })
    return affected


# ── STATION CONNECTIONS ───────────────────────────────────────────────────────

def query_station_connections(station_id: str) -> list[dict]:
    """
    List all direct connections from a given station.

    Args:
        station_id: e.g. "MS01" or "NR01"
    """
    cypher = """
        MATCH (start:Station {station_id: $station_id})-[r:METRO_LINK|RAIL_LINK|INTERCHANGE_TO]->(end:Station)
        RETURN end.station_id AS station_id, end.name AS name, type(r) AS connection_type, r.line AS line, coalesce(r.travel_time_min, 0) AS travel_time_min
    """
    conns = []
    with _driver() as driver:
        with driver.session() as session:
            result = session.run(cypher, station_id=station_id)
            for record in result:
                conns.append({
                    "station_id": record["station_id"],
                    "name": record["name"],
                    "connection_type": record["connection_type"],
                    "line": record["line"],
                    "travel_time_min": record["travel_time_min"]
                })
    return conns
