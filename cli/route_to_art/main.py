import click
import os
import sys
import functools
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable, TypeVar, cast

# Import custom error handling and logging
from route_to_art.config import Config
from route_to_art.exceptions import (
    RouteArtError, RouteParseError, ValidationError, 
    RenderingError, ExportError, ConfigError
)
from route_to_art.exporters import Exporter
from route_to_art.logging import (
    configure_for_cli, log_info, log_debug, log_error, 
    log_exception, log_warning, log_route_art_error
)
from route_to_art.models import Route
from route_to_art.parsers import RouteParser
from route_to_art.visualizer import RouteVisualizer

# Type variables for command handler decorator
T = TypeVar('T')
ClickContext = Any  # Type for Click context
CommandCallback = Callable[..., T]  # Type for command callbacks

# Define the package version - will be used in the CLI's version option
try:
    __version__ = version("route-to-art")
except Exception:
    __version__ = "0.1.0"  # Default fallback version


# Load and store configuration globally
_config = None

def get_config(config_path=None):
    """
    Get or initialize the configuration.
    
    Args:
        config_path: Optional path to a configuration file
        
    Returns:
        Config object
    """
    global _config
    if _config is None or config_path:
        _config = Config(config_path=config_path)
    return _config

def get_effective_options(config_path, option_dict):
    """
    Merge configuration and CLI options, with CLI options taking precedence.
    
    Args:
        config_path: Path to config file or None
        option_dict: Dictionary of CLI options
        
    Returns:
        Dictionary with effective options (config merged with CLI overrides)
    """
    # Load configuration
    config = get_config(config_path)
    defaults = config.get_defaults()
    
    # Start with configuration defaults
    result = {}
    
    # Map config keys to CLI option names
    config_to_cli = {
        "thickness": "thickness",
        "color": "color",
        "style": "style",
        "markers.enabled": "markers",
        "markers.unit": "markers_unit",
        "markers.interval": "marker_interval",
        "markers.size": "marker_size",
        "markers.color": "marker_color",
        "markers.label_font_size": "label_font_size",
        "overlay.enabled": "overlay",
        "overlay.position": "overlay_position",
        "overlay.font_size": "font_size",
        "overlay.font_color": "font_color",
        "overlay.background": "background",
        "overlay.bg_color": "bg_color",
        "overlay.bg_alpha": "bg_alpha",
        "export.formats": "formats",
        "export.width": None,  # Not directly mapped
        "export.height": None,  # Not directly mapped
        "export.dpi": "dpi",
        "export.page_size": "page_size"
    }
    
    # Handle markers enabled special case
    if "markers" in option_dict and option_dict["markers"] is False:
        result["markers"] = False
    elif defaults.get("markers", {}).get("enabled", False):
        result["markers"] = True
        
    # Fill in values from config, excluding None values and those
    # that will be overridden by CLI options
    for config_key, cli_key in config_to_cli.items():
        if cli_key is None:
            continue
            
        # Skip CLI options explicitly provided
        if cli_key in option_dict and option_dict[cli_key] is not None:
            continue
            
        # Special case for overlay fields
        if config_key == "overlay.enabled" and "overlay" in option_dict and option_dict["overlay"] is not None:
            continue
            
        # Get the config value using dot notation
        value = config.get(f"defaults.{config_key}")
        if value is not None:
            # Special handling for overlay fields
            if config_key == "overlay.fields" and value:
                result["overlay"] = ",".join(value)
            # Special handling for export formats
            elif config_key == "export.formats" and value:
                result["formats"] = ",".join(value)
            # Normal case
            elif cli_key:
                result[cli_key] = value
    
    # Override with CLI options (non-None values only)
    for key, value in option_dict.items():
        if value is not None:
            result[key] = value
    
    return result


def handle_command_errors(f: CommandCallback) -> CommandCallback:
    """
    Decorator to handle errors in CLI commands.
    
    This decorator wraps command functions to catch exceptions,
    log them appropriately, and display user-friendly error messages.
    
    Args:
        f: The command function to wrap
        
    Returns:
        Wrapped function with error handling
    """
    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return f(*args, **kwargs)
        except RouteArtError as e:
            # Log and display custom errors
            log_route_art_error(e, module=f.__name__)
            click.secho(str(e), fg="red")
            
            # Display suggestion if available
            if e.suggestion:
                click.echo(f"Suggestion: {e.suggestion}")
                
            # Exit with error code
            return 1
        except ExportError as e:
            # Handle export errors from the old format
            log_error(e, module=f.__name__)
            click.secho(f"Error: {str(e)}", fg="red")
            return 1
        except ConfigError as e:
            # Handle config errors from the old format
            log_error(e, module=f.__name__)
            click.secho(f"Configuration error: {str(e)}", fg="red")
            return 1
        except Exception as e:
            # Handle unexpected errors
            log_exception(e, f"Unexpected error in {f.__name__}", module=f.__name__)
            click.secho(f"Unexpected error: {str(e)}", fg="red")
            click.echo("This error has been logged. Please report this issue.")
            
            # In debug mode, show traceback
            if click.get_current_context().obj.get("debug"):
                click.echo("\nTraceback:")
                import traceback
                click.echo(traceback.format_exc())
                
            return 1
    
    return cast(CommandCallback, wrapper)


@click.group()
@click.version_option(version=__version__)
@click.option(
    "--config", 
    type=click.Path(exists=False), 
    help="Path to configuration file. CLI options override config values."
)
@click.option(
    "--verbose", "-v",
    count=True,
    help="Increase verbosity (can be used multiple times)"
)
@click.option(
    "--debug/--no-debug",
    default=False,
    help="Enable debug mode with detailed error information"
)
@click.option(
    "--no-log-file",
    is_flag=True,
    help="Disable logging to file"
)
@click.pass_context
def cli(ctx, config, verbose, debug, no_log_file):
    """Route-to-Art - Transform GPS routes into artwork."""
    # Initialize context
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["debug"] = debug
    
    # Set up logging
    configure_for_cli(
        verbosity=verbose,
        log_to_file=not no_log_file
    )
    
    log_info(f"Starting Route-to-Art v{__version__}")
    
    # Try to load config to catch any errors early
    try:
        if config:
            log_info(f"Loading configuration from {config}")
            get_config(config)
    except Exception as e:
        log_exception(e, "Error loading configuration")
        if isinstance(e, ConfigError):
            click.secho(f"Error loading configuration: {e}", fg="red")
        else:
            click.secho(f"Unexpected error loading configuration: {str(e)}", fg="red")
        ctx.exit(1)


@cli.command()
@click.argument("input_file", type=click.Path(exists=True))
@click.argument("output_file", type=click.Path())
@click.option("--color", help="Line color in hex format or named color (config override)")
@click.option(
    "--thickness",
    type=click.Choice(["thin", "medium", "thick"]),
    help="Line thickness (config override)"
)
@click.option(
    "--style",
    type=click.Choice(["solid", "dashed"]),
    help="Line style (config override)"
)
@click.option(
    "--dpi",
    type=int,
    help="Output resolution in dots per inch (PNG only, config override)"
)
@click.option(
    "--format",
    "formats",
    help="Output formats (comma-separated: png,svg,pdf). Defaults to file extension or config."
)
@click.option(
    "--page-size",
    type=click.Choice([
        "letter", "a4", 
        "square-small", "square-medium", "square-large",
        "landscape-small", "landscape-medium", "landscape-large"
    ]),
    help="Page size for PDF output (config override)"
)
@click.option(
    "--markers/--no-markers",
    help="Add distance markers along the route (config override)"
)
@click.option(
    "--markers-unit",
    type=click.Choice(["miles", "km"]),
    help="Unit for distance markers (config override)"
)
@click.option(
    "--marker-interval",
    type=float,
    help="Distance between markers in the specified unit (config override)"
)
@click.option(
    "--marker-size",
    type=float,
    help="Size of the marker points (config override)"
)
@click.option(
    "--marker-color",
    help="Color of markers (defaults to route color, config override)"
)
@click.option(
    "--label-font-size",
    type=int,
    help="Font size for distance labels (config override)"
)
@click.option(
    "--overlay",
    help="Information to overlay (comma-separated: distance,duration,elevation,name,date, config override)"
)
@click.option(
    "--overlay-position",
    type=click.Choice(["top-left", "top-right", "bottom-left", "bottom-right"]),
    help="Position of the information overlay (config override)"
)
@click.option(
    "--font-size",
    type=int,
    help="Font size for overlay text (config override)"
)
@click.option(
    "--font-color",
    help="Color for overlay text (config override)"
)
@click.option(
    "--background/--no-background",
    help="Add background box to overlay (config override)"
)
@click.option(
    "--bg-color",
    help="Color for overlay background (config override)"
)
@click.option(
    "--bg-alpha",
    type=float,
    help="Transparency of overlay background (0-1, config override)"
)
@click.pass_context
@handle_command_errors
def convert(
    ctx, input_file, output_file, color, thickness, style, dpi, formats, page_size,
    markers, markers_unit, marker_interval, marker_size, marker_color, label_font_size,
    overlay, overlay_position, font_size, font_color, background, bg_color, bg_alpha
):
    """
    Convert route data from a GPX-format file to artwork in PNG, SVG, or PDF format.
    
    CLI options override settings from the configuration file.
    Use --config to specify a configuration file.
    """
    log_info(f"Converting route from GPX file: {input_file} to {output_file}")
    log_debug("Convert options", data={
        "color": color,
        "thickness": thickness,
        "style": style,
        "formats": formats,
        "markers": markers
    })
    
    # Get effective options (config + CLI overrides)
    options = get_effective_options(
        ctx.obj.get("config_path"),
        {
            "color": color,
            "thickness": thickness,
            "style": style,
            "dpi": dpi,
            "formats": formats,
            "page_size": page_size,
            "markers": markers,
            "markers_unit": markers_unit,
            "marker_interval": marker_interval,
            "marker_size": marker_size,
            "marker_color": marker_color,
            "label_font_size": label_font_size,
            "overlay": overlay,
            "overlay_position": overlay_position,
            "font_size": font_size,
            "font_color": font_color,
            "background": background,
            "bg_color": bg_color,
            "bg_alpha": bg_alpha
        }
    )
    
    # Parse GPX file
    click.echo("Parsing route data from GPX-format file...")
    try:
        parser = RouteParser(input_file)
        route_data = parser.parse()
        
        if not route_data:
            error_msg = parser.get_error()
            raise RouteParseError.invalid_route_file(
                error_msg, 
                file_path=input_file
            )
        
        # Convert to internal model
        route = parser.to_route(route_data)
        
        # Check if route has any segments
        if not route.segments:
            raise RouteParseError.missing_track(file_path=input_file)
            
        # Check if route has any points
        if not route.get_total_points():
            raise RouteParseError.no_coordinates(file_path=input_file)
            
    except Exception as e:
        if not isinstance(e, RouteArtError):
            # Wrap non-RouteArtError exceptions
            raise RouteParseError.invalid_route_file(
                str(e), 
                file_path=input_file,
                original_error=e
            )
        raise
    
    # Create visualizer
    click.echo("Rendering route...")
    log_info("Creating visualization")
    
    try:
        visualizer = RouteVisualizer(route)
        visualizer.create_figure()
        
        # Render the route
        log_debug("Rendering route", data={
            "color": options.get("color"),
            "thickness": options.get("thickness"),
            "style": options.get("style")
        })
        
        visualizer.render_route(
            color=options.get("color"),
            thickness=options.get("thickness"),
            line_style=options.get("style")
        )
        
        # Add distance markers if requested
        if options.get("markers"):
            try:
                log_info("Adding distance markers")
                visualizer.add_distance_markers(
                    unit=options.get("markers_unit"),
                    interval=options.get("marker_interval"),
                    marker_size=options.get("marker_size"),
                    marker_color=options.get("marker_color"),
                    show_labels=True,
                    label_font_size=options.get("label_font_size")
                )
            except Exception as e:
                log_warning(f"Error adding markers: {str(e)}")
                click.secho(f"Error adding markers: {str(e)}", fg="yellow")
                click.echo("Continuing without markers...")
                
        # Add information overlay if requested
        if options.get("overlay"):
            try:
                log_info("Adding information overlay")
                # Parse overlay fields
                overlay_fields = [field.strip() for field in options.get("overlay").split(',')]
                
                visualizer.add_overlay(
                    fields=overlay_fields,
                    position=options.get("overlay_position"),
                    font_size=options.get("font_size")
                )
            except Exception as e:
                log_warning(f"Failed to add overlay: {e}")
    except Exception as e:
        log_error(f"Error rendering route: {e}")
        raise RenderingError(f"Failed to render route: {e}")
        
    # Determine export formats
    exporter = Exporter()
    output_path = Path(output_file)
    
    # If formats are explicitly provided, use those
    if formats:
        format_list = [f.strip() for f in formats.split(',')]
        try:
            click.echo(f"Exporting to {', '.join(format_list)} formats...")
            exported_files = exporter.export_multiple(
                figure=visualizer.get_figure(),
                base_path=output_path.with_suffix(''),  # Remove extension for base path
                formats=format_list,
                dpi=dpi,
                page_size=page_size
            )
        except ExportError as e:
            click.secho(f"Error: {str(e)}", fg="red")
            sys.exit(1)
    else:
        # Otherwise, determine format from file extension
        try:
            export_format = exporter.get_format(output_path)
            format_name = export_format.name.lower()
            click.echo(f"Exporting {format_name.upper()}...")
            
            # Export based on detected format
            if export_format == ExportFormat.PNG:
                exporter.export_png(
                    figure=visualizer.get_figure(),
                    output_path=output_path,
                    dpi=dpi
                )
            elif export_format == ExportFormat.SVG:
                exporter.export_svg(
                    figure=visualizer.get_figure(),
                    output_path=output_path
                )
            elif export_format == ExportFormat.PDF:
                exporter.export_pdf(
                    figure=visualizer.get_figure(),
                    output_path=output_path,
                    page_size=page_size
                )
            
            exported_files = [output_path]
            
        except ExportError as e:
            click.secho(f"Error: {str(e)}", fg="red")
            sys.exit(1)
    
    
    # Show style information
    style_info = []
    if color.startswith('#'):
        style_info.append(f"Color: {color}")
    else:
        style_info.append(f"Color: {color}")
    style_info.append(f"Thickness: {thickness}")
    style_info.append(f"Style: {style}")
    
    click.echo(f"Style: {', '.join(style_info)}")
    
    # Show marker information if enabled
    if markers:
        marker_info = []
        marker_info.append(f"Unit: {markers_unit}")
        if marker_interval:
            marker_info.append(f"Interval: {marker_interval} {markers_unit}")
        else:
            marker_info.append(f"Interval: 1.0 {markers_unit}")  # Default
        if marker_color:
            marker_info.append(f"Color: {marker_color}")
        marker_info.append(f"Size: {marker_size}")
        marker_info.append(f"Label size: {label_font_size}")
        
        click.echo(f"Markers: {', '.join(marker_info)}")
        
    # Show overlay information if enabled
    if overlay:
        overlay_info = []
        overlay_info.append(f"Fields: {overlay}")
        overlay_info.append(f"Position: {overlay_position}")
        overlay_info.append(f"Font size: {font_size}")
        overlay_info.append(f"Font color: {font_color}")
        overlay_info.append(f"Background: {'On' if background else 'Off'}")
        if background:
            overlay_info.append(f"BG color: {bg_color}")
            overlay_info.append(f"BG alpha: {bg_alpha}")
        
        click.echo(f"Overlay: {', '.join(overlay_info)}")


def validate_coordinates(route: Route) -> List[str]:
    """
    Validate that all coordinates in the route are within valid ranges.
    
    Args:
        route: The Route object to validate
        
    Returns:
        List of error messages, empty if no issues found
    """
    issues = []
    
    for i, segment in enumerate(route.segments):
        for j, point in enumerate(segment.points):
            # Latitude and longitude should already be validated on point creation,
            # but we can do additional checks here if needed
            if abs(point.latitude) > 85.0:
                issues.append(
                    f"Point {j+1} in segment {i+1} has extreme latitude ({point.latitude})"
                    " which may cause issues with map projections"
                )
    
    return issues


def validate_segments(route: Route) -> List[str]:
    """
    Validate segment integrity.
    
    Args:
        route: The Route object to validate
        
    Returns:
        List of error messages, empty if no issues found
    """
    issues = []
    
    # Check if there are any segments at all
    if not route.segments:
        issues.append("Route has no segments")
        return issues
    
    for i, segment in enumerate(route.segments):
        # Check if segment has points
        if not segment.points:
            issues.append(f"Segment {i+1} has no points")
            continue
        
        # Check if segment has enough points for meaningful data
        if len(segment.points) < 2:
            issues.append(f"Segment {i+1} has only one point - no route data")
            
        # Check for excessive points (warning, not error)
        if len(segment.points) > 10000:
            issues.append(f"Segment {i+1} has {len(segment.points)} points, "
                         "which may cause performance issues")
    
    return issues


def validate_timestamps(route: Route) -> List[str]:
    """
    Validate timestamp consistency.
    
    Args:
        route: The Route object to validate
        
    Returns:
        List of error messages, empty if no issues found
    """
    issues = []
    
    for i, segment in enumerate(route.segments):
        # Check if timestamps exist
        has_timestamps = any(p.timestamp for p in segment.points)
        if not has_timestamps:
            continue  # Skip further timestamp checks if no timestamps
            
        # Check if all points have timestamps
        missing_timestamps = any(p.timestamp is None for p in segment.points)
        if missing_timestamps:
            issues.append(f"Segment {i+1} has inconsistent timestamps "
                         "(some points missing timestamp data)")
            
        # Check for timestamp order
        timestamp_issues = False
        for j in range(1, len(segment.points)):
            curr = segment.points[j].timestamp
            prev = segment.points[j-1].timestamp
            if curr and prev and curr < prev:
                timestamp_issues = True
                break
                
        if timestamp_issues:
            issues.append(f"Segment {i+1} has out-of-order timestamps")
            
        # Check for duplicate timestamps
        timestamps = [p.timestamp for p in segment.points if p.timestamp]
        unique_timestamps = set(timestamps)
        if len(unique_timestamps) < len(timestamps):
            issues.append(f"Segment {i+1} has duplicate timestamps")
    
    return issues


@cli.command()
@click.argument("input_file", type=click.Path(exists=True))
def validate(input_file):
    """Validate route data from GPX file."""
    # Parse the GPX file
    parser = RouteParser(input_file)
    route_data = parser.parse()
    
    if not route_data:
        click.secho(f"Error: {parser.get_error()}", fg="red")
        sys.exit(1)
    
    # Convert to our model
    route = parser.to_route(route_data)
    
    # Run validation checks
    click.secho("\n=== Route Validation Results ===", fg="blue", bold=True)
    click.echo(f"File: {click.format_filename(os.path.abspath(input_file))}")
    
    # Run all validations
    segment_issues = validate_segments(route)
    coordinate_issues = validate_coordinates(route)
    timestamp_issues = validate_timestamps(route)
    
    # Combine all issues
    all_issues = segment_issues + coordinate_issues + timestamp_issues
    
    # Display validation status
    if not all_issues:
        click.secho("\n✓ Route data is valid", fg="green", bold=True)
        click.echo("No issues found")
        sys.exit(0)
    else:
        click.secho(f"\n✗ Found {len(all_issues)} issues in route data:", fg="red", bold=True)
        
        # Display issues by category
        if segment_issues:
            click.secho("\nSegment Issues:", fg="yellow")
            for issue in segment_issues:
                click.echo(f"- {issue}")
                
        if coordinate_issues:
            click.secho("\nCoordinate Issues:", fg="yellow")
            for issue in coordinate_issues:
                click.echo(f"- {issue}")
                
        if timestamp_issues:
            click.secho("\nTimestamp Issues:", fg="yellow")
            for issue in timestamp_issues:
                click.echo(f"- {issue}")
        
        click.echo("\nFix these issues to ensure proper processing of the route data.")
        sys.exit(1)


def format_distance(meters):
    """Format distance in both kilometers and miles."""
    km = meters / 1000
    miles = meters / 1609.34
    return f"{km:.2f} km ({miles:.2f} miles)"


def format_duration(duration):
    """Format timedelta for display."""
    if not duration:
        return "Unknown"
    
    days = duration.days
    hours, remainder = divmod(duration.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds and not (days or hours):
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    
    return ", ".join(parts)


def format_elevation(stats):
    """Format elevation statistics for display."""
    if not stats:
        return "No elevation data available"
    
    return (
        f"Min: {stats['min']:.1f}m, Max: {stats['max']:.1f}m, "
        f"Gain: {stats['gain']:.1f}m, Loss: {stats['loss']:.1f}m"
    )


def format_bounds(bounds):
    """Format geographic bounds for display."""
    min_lat, max_lat, min_lon, max_lon = bounds
    lat_range = f"{min_lat:.6f}° to {max_lat:.6f}°"
    lon_range = f"{min_lon:.6f}° to {max_lon:.6f}°"
    return f"Latitude: {lat_range}, Longitude: {lon_range}"


@cli.command()
@click.argument("input_file", type=click.Path(exists=True))
def info(input_file):
    """Display route information from GPX file."""
    # Parse the GPX file
    parser = RouteParser(input_file)
    route_data = parser.parse()
    
    if not route_data:
        click.secho(f"Error: {parser.get_error()}", fg="red")
        sys.exit(1)
    
    # Convert to our model
    route = parser.to_route(route_data)
    
    # Display file information
    click.secho("\n=== Route Information ===", fg="green", bold=True)
    click.echo(f"File: {click.format_filename(os.path.abspath(input_file))}")
    click.echo(f"Name: {route.name or 'Unnamed route'}")
    
    # Display basic stats
    click.secho("\n=== Route Statistics ===", fg="green", bold=True)
    click.echo(f"Distance: {format_distance(route.get_total_distance())}")
    
    duration = route.get_total_duration()
    click.echo(f"Duration: {format_duration(duration)}")
    
    # Display structure information
    click.secho("\n=== Route Structure ===", fg="green", bold=True)
    click.echo(f"Segments: {len(route.segments)}")
    click.echo(f"Points: {route.get_total_points()}")
    
    # Display elevation information if available
    click.secho("\n=== Elevation Profile ===", fg="green", bold=True)
    elevation_stats = route.get_elevation_stats()
    click.echo(format_elevation(elevation_stats))
    
    # Display geographic bounds
    click.secho("\n=== Geographic Bounds ===", fg="green", bold=True)
    click.echo(format_bounds(route.get_bounds()))
    
    click.echo("")  # Add a newline at the end


@cli.command()
@click.option(
    "--path",
    type=click.Path(exists=False),
    help="Path to create the config file. Defaults to ~/.route-to-art/config.yml"
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing config file if it exists"
)
def init_config(path, force):
    """Initialize a default configuration file."""
    config = Config()
    
    # Determine config path
    if not path:
        # Use default path
        path = config.get_default_path()
    
    # Check if file exists
    if os.path.exists(path) and not force:
        click.secho(f"Config file already exists at {path}", fg="yellow")
        click.echo("Use --force to overwrite")
        return 1
    
    # Ensure directory exists
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.exists(directory):
        try:
            os.makedirs(directory)
            click.echo(f"Created directory: {directory}")
        except OSError as e:
            click.secho(f"Error creating directory {directory}: {str(e)}", fg="red")
            return 1
    
    # Generate and write config
    sample_config = config.generate_sample()
    try:
        with open(path, 'w') as f:
            f.write(sample_config)
        click.secho(f"Config file created at {path}", fg="green")
        click.echo("You can now customize your configuration file.")
        return 0
    except OSError as e:
        click.secho(f"Error writing config file: {str(e)}", fg="red")
        return 1


if __name__ == "__main__":
    cli()

