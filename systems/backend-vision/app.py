#!/usr/bin/env python3
"""
Flask backend for generating image descriptions using Ollama vision models.
This service provides an API endpoint to generate descriptions for images.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import ollama
import os
import base64
import tempfile
import sqlite3
from pathlib import Path
from PIL import Image

app = Flask(__name__)
CORS(app)  # Enable CORS for frontend requests

# Configuration
#
# OLLAMA_HOST must be set when this service runs in a container: Ollama runs on
# the host, and "localhost" inside a container is the container itself, so the
# default would silently fail every request. Podman exposes the host as
# host.containers.internal. The ollama package also reads OLLAMA_HOST on its own,
# but the client is built explicitly here so the dependency is visible in the code
# rather than relying on library-internal behaviour.
OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://ollama.airlock.lan')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'gemma4:latest')  # Vision-capable model
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB max

ollama_client = ollama.Client(host=OLLAMA_HOST)

def _to_catalog_relative(abs_path, db_path):
    """
    Convert an absolute image path to the catalog-relative form stored in the DB.

    Since the relative-path migration, file_contents.file_path and
    alternative_versions.alternative_file_path hold paths relative to the catalog
    root (e.g. "Images/2026/08/29/x.jpg"). The catalog root is the directory that
    holds workflow_results.db, which is what db_path points at. Comparing an
    absolute path against those columns silently matches nothing - which is why
    people names had stopped appearing in generated descriptions.
    """
    catalog_root = os.path.dirname(os.path.abspath(db_path))
    abs_path = os.path.abspath(abs_path)
    try:
        rel = os.path.relpath(abs_path, catalog_root)
    except ValueError:
        return abs_path
    return abs_path if rel.startswith('..') else rel


def get_original_image_path(image_path, db_path):
    """
    Get the original image path if the given path is an alternate version.
    
    Args:
        image_path: Path to the image file (could be original or alternate)
        db_path: Path to the database
    
    Returns:
        str: Original image path (returns input path if not an alternate version)
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        # Check if this path is an alternate version
        cursor.execute(
            '''
            SELECT fc.file_path
            FROM alternative_versions av
            JOIN file_contents fc ON av.original_file_id = fc.id
            WHERE av.alternative_file_path = ? OR av.alternative_file_path = ?
            ''',
            (_to_catalog_relative(image_path, db_path), image_path)
        )
        row = cursor.fetchone()
        if row:
            original_path = row['file_path']
            print(f"Alternate version detected. Original path: {original_path}")
            return original_path
        else:
            # Not an alternate version, return the original path
            return image_path
    finally:
        conn.close()


def get_people_names_left_to_right(image_path):
    """
    Get tagged people in an image ordered from left to right based on face detections.
    
    Args:
        image_path: Path to the image file (can be original or alternate version)
    
    Returns:
        list[str]: Tagged person names ordered from left to right
    """
    image_path = os.path.abspath(image_path)
    current_dir = os.path.dirname(image_path)

    print("IMAGE TO BE CHECKED "+str(image_path))

    while True:
        db_path = os.path.join(current_dir, 'workflow_results.db')
        if os.path.exists(db_path):
            # Get the original image path if this is an alternate version
            original_image_path = get_original_image_path(image_path, db_path)
            
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.cursor()
                cursor.execute(
                    '''
                    SELECT fd.x, p.name AS person_name
                    FROM face_detections fd
                    JOIN people p ON fd.person_id = p.id
                    JOIN file_contents fc ON fd.file_id = fc.id
                    WHERE (fc.file_path = ? OR fc.file_path = ?)
                      AND fd.person_id IS NOT NULL
                      AND p.name IS NOT NULL
                    ORDER BY fd.x ASC, fd.y ASC, fd.id ASC
                    ''',
                    (_to_catalog_relative(original_image_path, db_path),
                     original_image_path)
                )
                rows = cursor.fetchall()
                ordered_people = []
                seen_names = set()

                for row in rows:
                    person_name = row['person_name']
                    if person_name and person_name not in seen_names:
                        ordered_people.append(person_name)
                        seen_names.add(person_name)

                return ordered_people
            finally:
                conn.close()

        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:
            break
        current_dir = parent_dir

    return []


def generate_description(image_path, people_names=None, location_name=None):
    """
    Generate a description for an image using Ollama vision model.
    
    Args:
        image_path: Path to the image file (can be original or alternate version)
        people_names: Optional list of people names present in the image
        location_name: Optional name of the location where the image was taken
    
    Returns:
        str: Generated description
    """
    temp_file = None
    try:
        # Verify the image file exists and is readable
        if not os.path.exists(image_path):
            raise Exception(f"Image file not found: {image_path}")
        
        # Check if file is readable
        if not os.access(image_path, os.R_OK):
            raise Exception(f"Image file is not readable: {image_path}")
        
        # Verify it's a valid image file by checking file signature
        try:
            with open(image_path, 'rb') as f:
                header = f.read(12)
                # Check for common image formats
                if not (
                    header.startswith(b'\xff\xd8\xff') or  # JPEG
                    header.startswith(b'\x89PNG') or       # PNG
                    header.startswith(b'GIF87a') or        # GIF
                    header.startswith(b'GIF89a') or        # GIF
                    header.startswith(b'RIFF') or          # WEBP
                    header.startswith(b'\x00\x00\x00')     # Some other formats
                ):
                    raise Exception(f"File does not appear to be a valid image format: {image_path}")
        except IOError as e:
            raise Exception(f"Cannot read image file: {str(e)}")
        
        # Re-encode the image through PIL to ensure Ollama compatibility
        # This fixes issues with HEIC-converted JPEGs that Ollama may reject
        try:
            print(f"Re-encoding image for Ollama compatibility: {image_path}")
            img = Image.open(image_path)
            
            # Convert to RGB if needed
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            
            # Resize if too large (max 2048px on longest side for better performance)
            max_size = 2048
            if max(img.size) > max_size:
                ratio = max_size / max(img.size)
                new_size = tuple(int(dim * ratio) for dim in img.size)
                img = img.resize(new_size, Image.Resampling.LANCZOS)
                print(f"Resized image from {image_path} to {new_size}")
            
            # Save to temporary file with optimized JPEG encoding
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
            temp_path = temp_file.name
            temp_file.close()
            
            img.save(temp_path, 'JPEG', quality=90, optimize=True)
            print(f"Re-encoded image saved to: {temp_path}")
            
            # Use the re-encoded image
            final_image_path = temp_path
            
        except Exception as e:
            print(f"Warning: Could not re-encode image, using original: {e}")
            final_image_path = image_path
        
        # Build the prompt
        prompt = "Give a detailed description of the image. Just use a very detailed descriptions, with no follow up questions at the end."

        switch_names_are_ordered = 0
        # IMPORTANT: Use original image path for face detection lookup
        # The image_path parameter might be an alternate version, but face detections
        # are stored against the original file path in the database
        ordered_people_names = get_people_names_left_to_right(image_path)

        if not ordered_people_names and people_names and len(people_names) > 0:
            ordered_people_names = people_names
        else:
            switch_names_are_ordered = 1

        if(switch_names_are_ordered==1):
            if ordered_people_names and len(ordered_people_names) > 0:
                people_str = ", ".join(ordered_people_names)
                prompt += f"\nClue: People present in the image from left to right: {people_str}."
        else:
            if ordered_people_names and len(ordered_people_names) > 0:
                people_str = ", ".join(ordered_people_names)
                prompt += f"\nClue: People present in the image: {people_str}."

        if location_name:
            prompt += f"\nClue: Location: {location_name}."
        print(prompt)
        # Call Ollama with the image
        response = ollama_client.chat(
            model=OLLAMA_MODEL,
            messages=[{
                'role': 'user',
                'content': prompt,
                'images': [final_image_path]
            }]
        )
        
        # Extract the description from the response
        description = response['message']['content']
        return description
    
    except Exception as e:
        raise Exception(f"Failed to generate description: {str(e)}")
    
    finally:
        # Clean up temporary file
        if temp_file and os.path.exists(temp_file.name):
            try:
                os.unlink(temp_file.name)
            except:
                pass


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'ok', 'model': OLLAMA_MODEL,
                    'ollama_host': OLLAMA_HOST}), 200


@app.route('/describe', methods=['POST'])
def describe_image():
    """
    Generate a description for an uploaded image.
    
    Expected JSON payload:
    {
        "image_path": "/path/to/image.jpg",
        "people_names": ["John", "Jane"],  // optional
        "location_name": "Central Park"    // optional
    }
    
    Returns:
    {
        "description": "Generated description text",
        "success": true
    }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'No JSON data provided', 'success': False}), 400
        
        image_path = data.get('image_path')
        if not image_path:
            return jsonify({'error': 'image_path is required', 'success': False}), 400
        
        # Log the request
        print(f"\n{'='*60}")
        print(f"Description request received:")
        print(f"  Image path: {image_path}")
        print(f"  File exists: {os.path.exists(image_path)}")
        if os.path.exists(image_path):
            print(f"  File size: {os.path.getsize(image_path)} bytes")
            print(f"  File readable: {os.access(image_path, os.R_OK)}")
        print(f"  People names: {data.get('people_names', [])}")
        print(f"  Location name: {data.get('location_name', 'None')}")
        print(f"{'='*60}\n")
        
        # Verify the image file exists
        if not os.path.exists(image_path):
            error_msg = f'Image file not found: {image_path}'
            print(f"ERROR: {error_msg}")
            return jsonify({'error': error_msg, 'success': False}), 404
        
        # Note: We don't check file size here because the generate_description
        # function will resize large images automatically before processing
        
        # Get optional people names and location name
        people_names = data.get('people_names', [])
        location_name = data.get('location_name')
        
        # Generate description
        print(f"Calling Ollama to generate description...")
        description = generate_description(image_path, people_names, location_name)
        print(f"Description generated successfully: {description[:100]}...")
        
        return jsonify({
            'description': description,
            'success': True
        }), 200
    
    except Exception as e:
        error_msg = str(e)
        print(f"\nERROR generating description: {error_msg}\n")
        return jsonify({
            'error': error_msg,
            'success': False
        }), 500


@app.route('/describe-base64', methods=['POST'])
def describe_image_base64():
    """
    Generate a description for a base64-encoded image.
    
    Expected JSON payload:
    {
        "image_base64": "base64_encoded_image_data",
        "people_names": ["John", "Jane"],  // optional
        "location_name": "Central Park"    // optional
    }
    
    Returns:
    {
        "description": "Generated description text",
        "success": true
    }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'No JSON data provided', 'success': False}), 400
        
        image_base64 = data.get('image_base64')
        if not image_base64:
            return jsonify({'error': 'image_base64 is required', 'success': False}), 400
        
        # Save base64 image to temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_file:
            tmp_path = tmp_file.name
            # Decode base64 and write to file
            image_data = base64.b64decode(image_base64)
            tmp_file.write(image_data)
        
        try:
            # Get optional people names and location name
            people_names = data.get('people_names', [])
            location_name = data.get('location_name')
            
            # Generate description
            description = generate_description(tmp_path, people_names, location_name)
            
            return jsonify({
                'description': description,
                'success': True
            }), 200
        
        finally:
            # Clean up temporary file
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    except Exception as e:
        return jsonify({
            'error': str(e),
            'success': False
        }), 500


if __name__ == '__main__':
    print("=" * 60)
    print("Image Description Backend")
    print("=" * 60)
    print(f"Model: {OLLAMA_MODEL}")
    print(f"Ollama host: {OLLAMA_HOST}")
    print("Endpoints:")
    print("  GET  /health - Health check")
    print("  POST /describe - Generate description from file path")
    print("  POST /describe-base64 - Generate description from base64")
    print("=" * 60)
    print()
    
    # Run the Flask app
    app.run(host='0.0.0.0', port=5003, debug=True)
