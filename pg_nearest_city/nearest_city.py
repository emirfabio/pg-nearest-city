import psycopg
import gzip

from typing import Optional
import importlib.resources


from psycopg import sql
from base_nearest_city import DbConfig
from pg_nearest_city.base_nearest_city import BaseNearestCity


class NearestCity:
    EXPECTED_CITY_COUNT = 153968

    def __init__(self, db: DbConfig | psycopg.Connection):
        """Initialize reverse geocoder with database configuration or existing connection.

        Args:
            db: Either a DbConfig object for new connections or an existing psycopg Connection
        """

        self.db = db
        self._connection = None
        self.is_external_connection = isinstance(db, psycopg.Connection)

        with importlib.resources.path(
            "pg_nearest_city.data", "cities_1000_simple.txt.gz"
        ) as cities_path:
            self.cities_file = cities_path
        with importlib.resources.path(
            "pg_nearest_city.data", "voronois.wkb.gz"
        ) as voronoi_path:
            self.voronoi_file = voronoi_path

    def _get_connection(self) -> psycopg.Connection:
        """Get or create database connection."""
        if self.is_external_connection:
            return self.db

        if self._connection is None or self._connection.closed:
            try:
                self._connection = psycopg.connect(self.db.get_connection_string())
            except Exception as e:
                raise RuntimeError(f"Database connection failed: {str(e)}")

        return self._connection

    def close(self) -> None:
        """Close the internal connection if we're managing it."""
        if not self.is_external_connection and self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "NearestCity":
        """Enable context manager support with automatic initialization."""
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Cleanup when used as context manager."""
        self.close()

    def query(self, lat: float, lon: float) -> Optional[Location]:
        """Find the nearest city to the given coordinates using Voronoi regions.

        Args:
            lat: Latitude in degrees (-90 to 90)
            lon: Longitude in degrees (-180 to 180)

        Returns:
            Location object if a matching city is found, None otherwise

        Raises:
            ValueError: If coordinates are out of valid ranges
            RuntimeError: If database query fails
        """

        # Validate coordinate ranges
        BaseNearestCity.validate_coordinates(lon, lat)

        try:
            conn = self._get_connection()
            with conn.cursor() as cur:
                cur.execute(BaseNearestCity._get_reverse_geocoding_query(lon, lat))
                result = cur.fetchone()

                if not result:
                    return None

                return Location(
                    city=result[0],
                    country=result[1],
                    lat=float(result[2]),
                    lon=float(result[3]),
                )
        except Exception as e:
            raise RuntimeError(f"Reverse geocoding failed: {str(e)}")

    def initialize(self) -> None:
        """Initialize the geocoding database with validation checks.

        Performs necessary initialization steps based on current database state.
        Will attempt repair if the database is in an invalid state.
        """
        try:
            conn = self._get_connection()
            with conn.cursor() as cur:
                status = self._check_initialization_status(cur)

                if status["is_initialized"]:
                    print("Database already properly initialized.")
                    return

                if status["needs_repair"]:
                    print(f"Database needs repair: {status['details']}")
                    print("Reinitializing from scratch...")
                    cur.execute("DROP TABLE IF EXISTS pg_nearest_city_geocoding;")

                print("Creating geocoding table...")
                self._create_geocoding_table(cur)

                print("Importing city data...")
                self._import_cities(cur)

                print("Processing Voronoi polygons...")
                self._import_voronoi_polygons(cur)

                print("Creating spatial index...")
                self._create_spatial_index(cur)

                conn.commit()

                # Verify initialization
                final_status = self._check_initialization_status(cur)
                if not final_status["is_initialized"]:
                    raise RuntimeError(
                        f"Initialization failed final validation: {final_status['details']}"
                    )

                print("Initialization complete and verified!")

        except Exception as e:
            raise RuntimeError(f"Database initialization failed: {str(e)}")

    def _check_initialization_status(self, cur) -> dict:
        """Check the status and integrity of the geocoding database.

        Performs essential validation checks to ensure the database is properly initialized
        and contains valid data.
        """
        # Check table existence
        cur.execute(BaseNearestCity._get_table_existance_query())
        table_exists = cur.fetchone()[0]

        if not table_exists:
            return {
                "is_initialized": False,
                "needs_repair": False,
                "details": "Table does not exist",
            }

        # Check table structure
        cur.execute(BaseNearestCity._get_table_structure_query())
        columns = {col: dtype for col, dtype in cur.fetchall()}
        expected_columns = {
            "city": "character varying",
            "country": "character varying",
            "lat": "numeric",
            "lon": "numeric",
            "geom": "geometry",
            "voronoi": "geometry",
        }

        if not all(col in columns for col in expected_columns):
            return {
                "is_initialized": False,
                "needs_repair": True,
                "details": "Table structure is invalid",
            }

        # Check data completeness
        cur.execute(BaseNearestCity._get_data_completeness_query())

        counts = cur.fetchone()
        total_cities, cities_with_voronoi = counts

        if total_cities == 0:
            return {
                "is_initialized": False,
                "needs_repair": False,
                "details": "No city data present",
            }

        if total_cities != self.EXPECTED_CITY_COUNT:
            return {
                "is_initialized": False,
                "needs_repair": True,
                "details": f"City count mismatch: expected {self.EXPECTED_CITY_COUNT}, found {total_cities}",
            }

        if cities_with_voronoi != total_cities:
            return {
                "is_initialized": False,
                "needs_repair": True,
                "details": "Missing Voronoi polygons",
            }

        # Check spatial index
        cur.execute(BaseNearestCity._get_spatial_index_check_query())
        has_index = cur.fetchone()[0]

        if not has_index:
            return {
                "is_initialized": False,
                "needs_repair": True,
                "details": "Missing spatial index",
            }

        # Everything looks good
        return {
            "is_initialized": True,
            "needs_repair": False,
            "details": "Database properly initialized",
        }

    def _import_cities(self, cur):
        if not self.cities_file.exists():
            raise FileNotFoundError(f"Cities file not found: {self.cities_file}")

        """Import city data using COPY protocol."""
        with cur.copy(
            "COPY pg_nearest_city_geocoding(city, country, lat, lon) FROM STDIN"
        ) as copy:
            with gzip.open(self.cities_file, "r") as f:
                copied_bytes = 0
                while data := f.read(8192):
                    copy.write(data)
                    copied_bytes += len(data)
                print(f"Imported {copied_bytes:,} bytes of city data")

    def _create_geocoding_table(self, cur):
        """Create the main table"""
        cur.execute("""
            CREATE TABLE pg_nearest_city_geocoding (
                city varchar,
                country varchar,
                lat decimal,
                lon decimal,
                geom geometry(Point,4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
                voronoi geometry(Polygon,4326)
            );
        """)

    def _import_voronoi_polygons(self, cur):
        """Import and integrate Voronoi polygons into the main table."""

        if not self.voronoi_file.exists():
            raise FileNotFoundError(f"Voronoi file not found: {self.voronoi_file}")

        # First create temporary table for the import
        cur.execute("""
            CREATE TEMP TABLE voronoi_import (
                city text,
                country text,
                wkb bytea
            );
        """)

        # Import the binary WKB data
        with cur.copy("COPY voronoi_import (city, country, wkb) FROM STDIN") as copy:
            with gzip.open(self.voronoi_file, "rb") as f:
                while data := f.read(8192):
                    copy.write(data)

        # Update main table with Voronoi geometries
        cur.execute("""
            UPDATE pg_nearest_city_geocoding g
            SET voronoi = ST_GeomFromWKB(v.wkb, 4326)
            FROM voronoi_import v
            WHERE g.city = v.city
            AND g.country = v.country;
        """)

        # Clean up temporary table
        cur.execute("DROP TABLE voronoi_import;")

    def _create_spatial_index(self, cur):
        """Create a spatial index on the Voronoi polygons for efficient queries."""
        cur.execute("""
            CREATE INDEX geocoding_voronoi_idx 
            ON pg_nearest_city_geocoding 
            USING GIST (voronoi);
        """)
