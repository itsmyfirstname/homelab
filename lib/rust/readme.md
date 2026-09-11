## Cargo
> Git files won’t be generated if you run cargo new within an existing Git repository; you can
override this behavior by using` cargo new --vcs=git`.


__Building a Cargo Project__

run `cargo build`, outputs to ./target/debug/hello_cargo

__Run A Cargo Project__

`cargo run`

__Release a Cargo Project__

When your project is finally ready for release, you can use cargo build --release to compile it with optimizations. This command will create an executable in target/release instead of target/debug. The optimizations make your Rust code run faster, but turning them on lengthens the time it takes for your program to compile.