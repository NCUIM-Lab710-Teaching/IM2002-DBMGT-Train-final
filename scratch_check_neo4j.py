import sys
from neo4j import GraphDatabase

URI = "bolt://localhost:7688"
AUTH = ("neo4j", "transitflow")

def run():
    with GraphDatabase.driver(URI, auth=AUTH) as driver:
        with driver.session() as session:
            res = session.run("CALL apoc.help('dijkstra') YIELD name RETURN name")
            names = [r["name"] for r in res]
            print("APOC dijkstra procedures:", names)

            res = session.run("MATCH ()-[r:METRO_LINK]->() RETURN keys(r) LIMIT 1")
            keys = [r[0] for r in res]
            print("METRO_LINK keys:", keys)

if __name__ == "__main__":
    run()
