"""EMI Analyzer, as a KiCad plugin.

A front end, not a second copy of the tool. The analysis, the database, the board files and
the Docker worker all belong to the EMI Analyzer desktop app, which is installed separately;
this plugin finds the app running on the same computer (or starts it), hands it the board
open in the PCB Editor, and shows the app's own pages in a window beside pcbnew.

Nothing is uploaded and nothing listens on a socket: the app serves the loopback interface
only, and the board never leaves this computer.
"""

__version__ = "0.1.0"
