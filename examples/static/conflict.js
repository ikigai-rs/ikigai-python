// A refused move is a 409 whose body is `conflict: {message}` as text/plain. htmx 2 treats a
// 4xx as no-swap plus an error, which would leave a player with a button that did nothing.
// This swaps the 409 into the target the button names (the board's status region, which is
// role=status and so announced) as TEXT, never as HTML, and says it is not a failure. The
// board is left as it was: the move did not happen.
document.body.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail;
    if (detail.xhr && detail.xhr.status === 409 && detail.target) {
        detail.shouldSwap = false;
        detail.isError = false;
        detail.target.textContent = detail.xhr.responseText;
    }
});
