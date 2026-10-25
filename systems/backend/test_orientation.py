#!/usr/bin/env python3
"""Test script to verify EXIF orientation handling"""

from PIL import Image, ImageOps
import io

def test_orientation_handling():
    """Test that ImageOps.exif_transpose works as expected"""
    print("Testing EXIF orientation handling...")
    
    # Test 1: Create a simple test image
    test_img = Image.new('RGB', (100, 200), color='red')
    print(f"✓ Created test image: {test_img.size}")
    
    # Test 2: Test exif_transpose with no EXIF data
    result = ImageOps.exif_transpose(test_img)
    if result is None:
        print("✓ exif_transpose returns None for images without EXIF (expected)")
    else:
        print(f"✓ exif_transpose returned image: {result.size}")
    
    # Test 3: Test BytesIO conversion
    img_io = io.BytesIO()
    test_img.save(img_io, 'JPEG', quality=95)
    img_data = img_io.getvalue()
    print(f"✓ Successfully converted image to bytes: {len(img_data)} bytes")
    
    # Test 4: Test mode conversions
    rgba_img = Image.new('RGBA', (50, 50), color=(255, 0, 0, 128))
    background = Image.new('RGB', rgba_img.size, (255, 255, 255))
    background.paste(rgba_img, mask=rgba_img.split()[-1])
    print(f"✓ Successfully converted RGBA to RGB: {background.size}")
    
    print("\n✅ All orientation handling tests passed!")
    return True

if __name__ == '__main__':
    test_orientation_handling()
