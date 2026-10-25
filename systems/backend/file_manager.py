import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import Optional
from pathlib import Path


class FileWriteStrategy(ABC):
    """Abstract base class for file writing strategies"""
    
    @abstractmethod
    def write(self, source: str, destination: str, base_folder: Optional[str] = None) -> bool:
        """
        Write file from source to destination
        
        Args:
            source: Source file path
            destination: Destination file path
            base_folder: Base folder to prepend to destination path
        """
        pass


class LocalCopyStrategy(FileWriteStrategy):
    """Strategy for copying files locally using system copy"""
    
    def write(self, source: str, destination: str, base_folder: Optional[str] = None) -> bool:
        """
        Copy file locally using shutil
        
        Args:
            source: Source file path
            destination: Destination file path or directory
            base_folder: Base folder to prepend to destination path
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Ensure source exists
            if not os.path.exists(source):
                raise FileNotFoundError(f"Source file not found: {source}")
            
            # Prepend base_folder to destination if provided
            if base_folder:
                destination = os.path.join(base_folder, destination)
            
            # If destination is a directory, preserve filename
            if os.path.isdir(destination):
                filename = os.path.basename(source)
                destination = os.path.join(destination, filename)
            
            # Create destination directory if it doesn't exist
            dest_dir = os.path.dirname(destination)
            if dest_dir and not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)
            
            shutil.copy2(source, destination)
            print(f"Successfully copied {source} to {destination}")
            return True
        except Exception as e:
            print(f"Error copying file locally: {e}")
            return False


class SCPStrategy(FileWriteStrategy):
    """Strategy for copying files via SCP (Secure Copy Protocol)"""
    
    def __init__(self, username: str, host: str, port: int = 22):
        """
        Initialize SCP strategy
        
        Args:
            username: Remote username
            host: Remote host address
            port: SSH port (default: 22)
        """
        self.username = username
        self.host = host
        self.port = port
    
    def write(self, source: str, destination: str, base_folder: Optional[str] = None) -> bool:
        """
        Copy file via SCP
        
        Args:
            source: Local source file path
            destination: Remote destination path
            base_folder: Base folder to prepend to destination path
            
        Returns:
            bool: True if successful, False otherwise
        """
        print("*** Entering the procedure Copying file via SCP")

        try:
            print("*** Copying file via SCP...")
            if not os.path.exists(source):
                print("path not found")
                raise FileNotFoundError(f"Source file not found: {source}")
            
            # Prepend base_folder to destination if provided
            if base_folder:
                destination = f"{base_folder}/{destination}".replace('//', '/')
            
            remote_path = f"{self.username}@{self.host}:{destination}"
            remote_user = f"{self.username}@{self.host}"
            mkdir_cmd:str = f"mkdir -p {os.path.dirname(destination)}"
            cmd1: list[str] = ["ssh",remote_user, mkdir_cmd]
            result1: str = subprocess.run(cmd1, capture_output=True, text=True)
            if result1.returncode != 0:
                print(f"Error creating directory: {result1.stderr}")
                return False

            cmd = ["scp", "-P", str(self.port), source, remote_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                print(f"Successfully copied {source} to {remote_path} via SCP")
                return True
            else:
                print(f"SCP error: {result.stderr}")
                return False
        except Exception as e:
            print(f"Error copying file via SCP: {e}")
            return False


class FTPStrategy(FileWriteStrategy):
    """Strategy for copying files via FTP"""
    
    def __init__(self, host: str, username: str, password: str, port: int = 21):
        """
        Initialize FTP strategy
        
        Args:
            host: FTP server address
            username: FTP username
            password: FTP password
            port: FTP port (default: 21)
        """
        self.host = host
        self.username = username
        self.password = password
        self.port = port
    
    def write(self, source: str, destination: str, base_folder: Optional[str] = None) -> bool:
        """
        Copy file via FTP
        
        Args:
            source: Local source file path
            destination: Remote destination path
            base_folder: Base folder to prepend to destination path
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            from ftplib import FTP
            
            if not os.path.exists(source):
                raise FileNotFoundError(f"Source file not found: {source}")
            
            # Prepend base_folder to destination if provided
            if base_folder:
                destination = f"{base_folder}/{destination}".replace('//', '/')
            
            # Connect to FTP server
            ftp = FTP()
            ftp.connect(self.host, self.port)
            ftp.login(self.username, self.password)
            
            # Change to destination directory if needed
            dest_dir = os.path.dirname(destination)
            if dest_dir:
                try:
                    ftp.cwd(dest_dir)
                except:
                    # Create directory if it doesn't exist
                    self._create_remote_dirs(ftp, dest_dir)
                    ftp.cwd(dest_dir)
            
            # Upload file
            filename = os.path.basename(destination) or os.path.basename(source)
            with open(source, 'rb') as file:
                ftp.storbinary(f'STOR {filename}', file)
            
            ftp.quit()
            print(f"Successfully uploaded {source} to {self.host}:{destination} via FTP")
            return True
        except Exception as e:
            print(f"Error copying file via FTP: {e}")
            return False
    
    def _create_remote_dirs(self, ftp, path: str):
        """Create remote directories recursively"""
        dirs = path.split('/')
        for dir_name in dirs:
            if dir_name:
                try:
                    ftp.mkd(dir_name)
                except:
                    pass
                try:
                    ftp.cwd(dir_name)
                except:
                    pass


class BoxStrategy(FileWriteStrategy):
    """Strategy for copying files to Box cloud storage"""
    
    def __init__(self, client_id: str, client_secret: str, access_token: Optional[str] = None):
        """
        Initialize Box strategy
        
        Args:
            client_id: Box application client ID
            client_secret: Box application client secret
            access_token: Box access token (optional, can use OAuth)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self._client = None
    
    def _get_client(self):
        """Get or create Box client"""
        if self._client is None:
            try:
                from boxsdk import OAuth2, Client
                
                if self.access_token:
                    oauth = OAuth2(
                        client_id=self.client_id,
                        client_secret=self.client_secret,
                        access_token=self.access_token
                    )
                else:
                    # For production, implement proper OAuth flow
                    raise ValueError("Access token required for Box authentication")
                
                self._client = Client(oauth)
            except ImportError:
                raise ImportError("boxsdk package required. Install with: pip install boxsdk")
        
        return self._client
    
    def write(self, source: str, destination: str, base_folder: Optional[str] = None) -> bool:
        """
        Upload file to Box
        
        Args:
            source: Local source file path
            destination: Remote destination path (folder_id/filename or just filename for root)
            base_folder: Base folder to prepend to destination path
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            if not os.path.exists(source):
                raise FileNotFoundError(f"Source file not found: {source}")
            
            client = self._get_client()
            
            # Prepend base_folder to destination if provided
            if base_folder:
                destination = f"{base_folder}/{destination}".replace('//', '/')
            
            # Parse destination to get folder_id and filename
            parts = destination.split('/')
            if len(parts) > 1:
                folder_id = parts[0]
                filename = '/'.join(parts[1:])
            else:
                folder_id = '0'  # Root folder
                filename = destination or os.path.basename(source)
            
            # Upload file
            folder = client.folder(folder_id)
            with open(source, 'rb') as file:
                uploaded_file = folder.upload_stream(file, filename)
            
            print(f"Successfully uploaded {source} to Box (ID: {uploaded_file.id})")
            return True
        except Exception as e:
            print(f"Error uploading file to Box: {e}")
            return False


class S3Strategy(FileWriteStrategy):
    """Strategy for copying files to Amazon S3 cloud storage"""
    
    def __init__(self, bucket_name: str, aws_access_key_id: Optional[str] = None,
                 aws_secret_access_key: Optional[str] = None, region_name: Optional[str] = None):
        """
        Initialize S3 strategy
        
        Args:
            bucket_name: S3 bucket name
            aws_access_key_id: AWS access key ID (optional, can use environment variables or IAM roles)
            aws_secret_access_key: AWS secret access key (optional)
            region_name: AWS region name (optional, e.g., 'us-east-1')
        """
        self.bucket_name = bucket_name
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.region_name = region_name
        self._s3_client = None
    
    def _get_s3_client(self):
        """Get or create S3 client"""
        if self._s3_client is None:
            try:
                import boto3
                
                # Create S3 client with provided credentials or use default credential chain
                if self.aws_access_key_id and self.aws_secret_access_key:
                    self._s3_client = boto3.client(
                        's3',
                        aws_access_key_id=self.aws_access_key_id,
                        aws_secret_access_key=self.aws_secret_access_key,
                        region_name=self.region_name
                    )
                else:
                    # Use default credential chain (environment variables, IAM roles, etc.)
                    self._s3_client = boto3.client('s3', region_name=self.region_name)
                    
            except ImportError:
                raise ImportError("boto3 package required. Install with: pip install boto3")
        
        return self._s3_client
    
    def write(self, source: str, destination: str, base_folder: Optional[str] = None) -> bool:
        """
        Upload file to S3 bucket
        
        Args:
            source: Local source file path
            destination: S3 object key (path within bucket, e.g., 'folder/file.txt')
            base_folder: Base folder to prepend to destination path
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            if not os.path.exists(source):
                raise FileNotFoundError(f"Source file not found: {source}")
            
            s3_client = self._get_s3_client()
            
            # Use basename if destination is empty
            s3_key = destination or os.path.basename(source)
            
            # Prepend base_folder to s3_key if provided
            if base_folder:
                s3_key = f"{base_folder}/{s3_key}".replace('//', '/')
            
            # Upload file to S3
            with open(source, 'rb') as file:
                s3_client.upload_fileobj(file, self.bucket_name, s3_key)
            
            s3_url = f"s3://{self.bucket_name}/{s3_key}"
            print(f"Successfully uploaded {source} to {s3_url}")
            return True
        except Exception as e:
            print(f"Error uploading file to S3: {e}")
            return False
    
    def upload_with_metadata(self, source: str, destination: str, 
                            metadata: Optional[dict] = None,
                            content_type: Optional[str] = None) -> bool:
        """
        Upload file to S3 with custom metadata and content type
        
        Args:
            source: Local source file path
            destination: S3 object key
            metadata: Custom metadata dictionary
            content_type: MIME type (e.g., 'image/jpeg', 'application/pdf')
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            if not os.path.exists(source):
                raise FileNotFoundError(f"Source file not found: {source}")
            
            s3_client = self._get_s3_client()
            s3_key = destination or os.path.basename(source)
            
            # Prepare extra arguments
            extra_args = {}
            if metadata:
                extra_args['Metadata'] = metadata
            if content_type:
                extra_args['ContentType'] = content_type
            
            # Upload file
            s3_client.upload_file(source, self.bucket_name, s3_key, ExtraArgs=extra_args)
            
            s3_url = f"s3://{self.bucket_name}/{s3_key}"
            print(f"Successfully uploaded {source} to {s3_url} with metadata")
            return True
        except Exception as e:
            print(f"Error uploading file to S3 with metadata: {e}")
            return False


class FileManager:
    """
    File manager class with methods to read and write files using different strategies
    """
    
    def __init__(self, base_folder: Optional[str] = None):
        """
        Initialize FileManager
        
        Args:
            base_folder: Base folder where files will be written. If provided, this will be
                        prepended to all destination paths when using write strategies.
        """
        self.base_folder = base_folder
        self._write_strategy: Optional[FileWriteStrategy] = None
    
    def write_file(self, source: str, destination: str,
                   strategy: Optional[FileWriteStrategy] = None) -> bool:
        """
        Write file from source to destination using specified strategy
        
        Args:
            source: Source file path
            destination: Destination path
            strategy: FileWriteStrategy instance (defaults to LocalCopyStrategy)
            
        Returns:
            bool: True if successful, False otherwise
        """
        if strategy is None:
            strategy = LocalCopyStrategy()
        
        return strategy.write(source, destination, self.base_folder)
    
    def write_file_local(self, source: str, destination: str) -> bool:
        """
        Write file locally using system copy
        
        Args:
            source: Source file path
            destination: Destination path or directory
            
        Returns:
            bool: True if successful, False otherwise
        """
        strategy = LocalCopyStrategy()
        return strategy.write(source, destination, self.base_folder)
    
    def write_file_scp(self, source: str, destination: str,
                       username: str, host: str, port: int = 22) -> bool:
        """
        Write file to remote location via SCP
        
        Args:
            source: Local source file path
            destination: Remote destination path
            username: Remote username
            host: Remote host address
            port: SSH port (default: 22)
            
        Returns:
            bool: True if successful, False otherwise
        """
        strategy = SCPStrategy(username, host, port)
        return strategy.write(source, destination, self.base_folder)
    
    def write_file_ftp(self, source: str, destination: str,
                       host: str, username: str, password: str, port: int = 21) -> bool:
        """
        Write file to remote location via FTP
        
        Args:
            source: Local source file path
            destination: Remote destination path
            host: FTP server address
            username: FTP username
            password: FTP password
            port: FTP port (default: 21)
            
        Returns:
            bool: True if successful, False otherwise
        """
        strategy = FTPStrategy(host, username, password, port)
        return strategy.write(source, destination, self.base_folder)
    
    def write_file_box(self, source: str, destination: str,
                       client_id: str, client_secret: str,
                       access_token: Optional[str] = None) -> bool:
        """
        Write file to Box cloud storage
        
        Args:
            source: Local source file path
            destination: Remote destination path (folder_id/filename)
            client_id: Box application client ID
            client_secret: Box application client secret
            access_token: Box access token
            
        Returns:
            bool: True if successful, False otherwise
        """
        strategy = BoxStrategy(client_id, client_secret, access_token)
        return strategy.write(source, destination, self.base_folder)
    
    def write_file_s3(self, source: str, destination: str,
                      bucket_name: str, aws_access_key_id: Optional[str] = None,
                      aws_secret_access_key: Optional[str] = None,
                      region_name: Optional[str] = None) -> bool:
        """
        Write file to Amazon S3 bucket
        
        Args:
            source: Local source file path
            destination: S3 object key (path within bucket)
            bucket_name: S3 bucket name
            aws_access_key_id: AWS access key ID (optional)
            aws_secret_access_key: AWS secret access key (optional)
            region_name: AWS region name (optional, e.g., 'us-east-1')
            
        Returns:
            bool: True if successful, False otherwise
        """
        strategy = S3Strategy(bucket_name, aws_access_key_id, aws_secret_access_key, region_name)
        return strategy.write(source, destination, self.base_folder)
    
    def read_file(self, filepath: str, mode: str = 'r', encoding: str = 'utf-8') -> Optional[str]:
        """
        Read file contents
        
        Args:
            filepath: Path to file to read
            mode: File open mode ('r' for text, 'rb' for binary)
            encoding: Text encoding (default: utf-8)
            
        Returns:
            File contents as string (text mode) or bytes (binary mode), None if error
        """
        try:
            if not os.path.exists(filepath):
                raise FileNotFoundError(f"File not found: {filepath}")
            
            if 'b' in mode:
                with open(filepath, mode) as f:
                    content = f.read()
            else:
                with open(filepath, mode, encoding=encoding) as f:
                    content = f.read()
            
            print(f"Successfully read {filepath}")
            return content
        except Exception as e:
            print(f"Error reading file: {e}")
            return None
    
    def read_file_lines(self, filepath: str, encoding: str = 'utf-8') -> Optional[list]:
        """
        Read file contents as list of lines
        
        Args:
            filepath: Path to file to read
            encoding: Text encoding (default: utf-8)
            
        Returns:
            List of lines, None if error
        """
        try:
            if not os.path.exists(filepath):
                raise FileNotFoundError(f"File not found: {filepath}")
            
            with open(filepath, 'r', encoding=encoding) as f:
                lines = f.readlines()
            
            print(f"Successfully read {len(lines)} lines from {filepath}")
            return lines
        except Exception as e:
            print(f"Error reading file: {e}")
            return None


# Example usage
if __name__ == "__main__":
    # Create FileManager instance
    fm = FileManager()
    
    # Example 1: Local copy
    print("\n=== Example 1: Local Copy ===")
    # fm.write_file_local("source.txt", "destination.txt")
    
    # Example 2: SCP copy
    print("\n=== Example 2: SCP Copy ===")
    # fm.write_file_scp("source.txt", "/remote/path/file.txt", 
    #                   username="user", host="example.com")
    
    # Example 3: FTP copy
    print("\n=== Example 3: FTP Copy ===")
    # fm.write_file_ftp("source.txt", "/remote/path/file.txt",
    #                   host="ftp.example.com", username="user", password="pass")
    
    # Example 4: Box upload
    print("\n=== Example 4: Box Upload ===")
    # fm.write_file_box("source.txt", "0/file.txt",
    #                   client_id="your_client_id", 
    #                   client_secret="your_client_secret",
    #                   access_token="your_access_token")
    
    # Example 5: S3 upload
    print("\n=== Example 5: S3 Upload ===")
    # fm.write_file_s3("source.txt", "folder/file.txt",
    #                  bucket_name="my-bucket",
    #                  aws_access_key_id="your_access_key",
    #                  aws_secret_access_key="your_secret_key",
    #                  region_name="us-east-1")
    
    # Example 6: Read file
    print("\n=== Example 6: Read File ===")
    # content = fm.read_file("source.txt")
    # if content:
    #     print(f"File content: {content[:100]}...")
    
    print("\nAll examples are commented out. Uncomment and provide actual paths/credentials to test.")
