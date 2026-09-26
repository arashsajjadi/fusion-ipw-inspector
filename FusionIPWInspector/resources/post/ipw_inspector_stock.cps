/**
  Fusion IPW Inspector - in-process stock export post.
  Version: 2

  This post produces no NC code. It exists so that Fusion's post-processing
  engine exports the setup's stock (for "From preceding setup" setups this is
  the in-process stock left by the preceding setups) and the part as STL, the
  same mechanism machine-simulation posts such as camplete.cps rely on.
  The engine hands the file paths over as global parameters at onClose; this
  post copies the files next to its own output and writes a small key=value
  sidecar that the add-in reads.

  Installed automatically by the add-in into the personal post folder. It is
  safe to delete; the add-in re-creates it when needed.
*/
description = "IPW Inspector stock export";
vendor = "Fusion IPW Inspector";
vendorUrl = "https://github.com/arashsajjadi/fusion-ipw-inspector";
legal = "MIT License";
certificationLevel = 2;
minimumRevision = 45917;

longDescription = "Helper post used by the Fusion IPW Inspector add-in to export the in-process stock as STL. It writes no NC code.";

extension = "ipwinfo";
setCodePage("ascii");

capabilities = CAPABILITY_MILLING | CAPABILITY_TURNING | CAPABILITY_INTERMEDIATE;
tolerance = spatial(0.002, MM);
minimumChordLength = spatial(0.25, MM);
minimumCircularRadius = spatial(0.01, MM);
maximumCircularRadius = spatial(1000, MM);
minimumCircularSweep = toRad(0.01);
maximumCircularSweep = toRad(180);
allowHelicalMoves = true;
allowedCircularPlanes = undefined;
allowSpiralMoves = true;

properties = {
  exportStock: {
    title      : "Export stock",
    description: "Ask Fusion to export the setup stock (in-process stock) as STL.",
    group      : "preferences",
    type       : "boolean",
    value      : true,
    scope      : "post"
  },
  exportPart: {
    title      : "Export part",
    description: "Ask Fusion to export the part as STL (used for validation).",
    group      : "preferences",
    type       : "boolean",
    value      : true,
    scope      : "post"
  }
};
groupDefinitions = {
  preferences: {title:"Preferences", description:"Export switches", order:0}
};

this.exportStock = true;
this.exportPart = true;
this.exportFixture = false;

var sectionCount = 0;

function keyValue(key, value) {
  writeln(key + "=" + value);
}

function globalOrEmpty(name) {
  return hasGlobalParameter(name) ? getGlobalParameter(name) : "";
}

function onOpen() {
  keyValue("format", "ipw-inspector-1");
  keyValue("post-version", "2");
  keyValue("unit", unit == MM ? "mm" : "in");
  keyValue("document", globalOrEmpty("document-path"));
  keyValue("document-id", globalOrEmpty("document-id"));
  keyValue("setup", globalOrEmpty("job-description"));
  keyValue("stock-type", globalOrEmpty("stock-type"));
  keyValue("stock-lower-x", globalOrEmpty("stock-lower-x"));
  keyValue("stock-upper-x", globalOrEmpty("stock-upper-x"));
  keyValue("stock-lower-y", globalOrEmpty("stock-lower-y"));
  keyValue("stock-upper-y", globalOrEmpty("stock-upper-y"));
  keyValue("stock-lower-z", globalOrEmpty("stock-lower-z"));
  keyValue("stock-upper-z", globalOrEmpty("stock-upper-z"));
}

function onSection() {
  sectionCount += 1;
  skipRemainingSection();
}

function onClose() {
  var outputPath = getOutputPath();
  var folder = FileSystem.getFolderPath(outputPath);
  var base = FileSystem.getFilename(outputPath);
  var dot = base.lastIndexOf(".");
  if (dot > 0) {
    base = base.substring(0, dot);
  }
  var stockPath = globalOrEmpty("autodeskcam:stock-path");
  var partPath = globalOrEmpty("autodeskcam:part-path");
  var stockDest = "";
  var partDest = "";
  if (stockPath && FileSystem.isFile(stockPath)) {
    stockDest = FileSystem.getCombinedPath(folder, base + "_stock.stl");
    FileSystem.copyFile(stockPath, stockDest);
  }
  if (partPath && FileSystem.isFile(partPath)) {
    partDest = FileSystem.getCombinedPath(folder, base + "_part.stl");
    FileSystem.copyFile(partPath, partDest);
  }
  keyValue("sections", sectionCount);
  keyValue("stock-file", stockDest);
  keyValue("part-file", partDest);
  keyValue("status", stockDest ? "ok" : "no-stock");
}
