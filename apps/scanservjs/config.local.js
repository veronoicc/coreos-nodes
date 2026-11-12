// scanservjs 3.3.0 local config (read once at start, see docs/10-configuration.md). Plain objects only: no require.
module.exports = {
  afterConfig(config) {
    // none + manual (one PDF from several pages: place a page, scan, repeat, finish). The auto modes are for an ADF; this
    // scanner is a flatbed only. Strings are Constants.BATCH_NONE / BATCH_MANUAL (src/constants.js:2-3).
    config.batchModes = ['none', 'manual'];

    // Same shape as the stock "PDF (JPG | high quality)" pipeline (src/classes/config.js:194-202): every scanned TIF to a
    // JPG, `convert @-` makes one PDF of them, the last command lists the file to keep. The Gray one adds
    // -colorspace Gray (smaller JPGs). No OCR pipelines: Paperless does the OCR. The first entry is the default
    // (src/classes/context.js:44).
    config.pipelines = [
      {
        extension: 'pdf',
        description: 'PDF Color (JPG | @:pipeline.high-quality)',
        commands: [
          'while read filename ; do convert -quality 92 $filename converted-$(date +%s.%N).jpg ; done && ls converted-*.jpg',
          'convert @- scan-0000.pdf',
          'ls scan-*.*'
        ]
      },
      {
        extension: 'pdf',
        description: 'PDF Gray (JPG | @:pipeline.high-quality)',
        commands: [
          'while read filename ; do convert -quality 92 -colorspace Gray $filename converted-$(date +%s.%N).jpg ; done && ls converted-*.jpg',
          'convert @- scan-0000.pdf',
          'ls scan-*.*'
        ]
      }
    ];
  },

  // Scan defaults, set on whatever the scanner exposes (the feature names are the scanimage options, see the example in
  // config.default.js:103-115): 300 dpi, Color, A4 (the platen is 215.9 x 297 mm). The resolution list stays as the device reports it.
  afterDevices(devices) {
    for (const device of devices) {
      const f = device.features;
      if (f['--mode']) f['--mode'].default = 'Color';
      if (f['--resolution']) f['--resolution'].default = 300;
      if (f['-x']) f['-x'].default = 210;
      if (f['-y']) f['-y'].default = 297;
    }
  }
};
