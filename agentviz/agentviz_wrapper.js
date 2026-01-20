const async_hooks = require('async_hooks');
const child_process = require('child_process');
const fs = require('fs');

// Store original functions
const originalSpawn = child_process.spawn;
const originalExec = child_process.exec;
const originalExecFile = child_process.execFile;

const emitToolEvent = (command, args) => {
    const event = {
        command: command,
        arguments: args,
        timestamp: new Date().toISOString(),
    };
    console.log(`__AGENTVIZ_TOOL__${JSON.stringify(event)}`);
};

child_process.spawn = function (command, args, options) {
    emitToolEvent(command, args);
    return originalSpawn.apply(this, arguments);
};

child_process.exec = function (command, options, callback) {
    emitToolEvent(command, []);
    return originalExec.apply(this, arguments);
};

child_process.execFile = function (file, args, options, callback) {
    emitToolEvent(file, args);
    return originalExecFile.apply(this, arguments);
};
